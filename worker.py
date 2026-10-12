"""Bounded, resumable, cancellable background workers backed by the SQLite job table.

The queue lives in the database, not in memory, so pending work survives restarts and
memory use does not grow with queue length. A fixed number of threads claim jobs one at
a time. Running jobs hold a heartbeat lease; on start-up, jobs whose lease went stale
(crash, kill, power loss) are re-queued, and repeatedly interrupted jobs are failed so a
poison clip cannot crash-loop the queue.
"""
import os
import shutil
import socket
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from PIL import Image

import color
import core

MAX_WORKERS = 8
DEFAULT_MIN_FREE_BYTES = 2 * 1024 ** 3


class Cancelled(Exception):
    """The user cancelled this job."""


class Interrupted(Exception):
    """The pool is shutting down; the job goes back to the queue."""


class Skipped(Exception):
    """The job no longer applies (e.g. the clip was excluded after queueing)."""


def ensure_free_space(folder, min_free_bytes):
    free = shutil.disk_usage(folder).free
    if free < min_free_bytes:
        raise OSError(f'Low disk space for the preview cache: {free // 1024 ** 2} MB free, '
                      f'{min_free_bytes // 1024 ** 2} MB required.')


def analyze_clip(store, job, pool, checkpoint):
    """Probe a clip, resolve its color profile and decode uniform preview samples,
    converted to Rec.709, into the project cache."""
    clip = store.clip(job['clip_id'])
    if clip['excluded']:
        raise Skipped('Clip was excluded after it was queued.')
    source = {'path': clip['path'], 'size': clip['size'], 'mtime_ns': clip['mtime_ns']}
    core.verify_source(source)
    count = int(job['params']['sample_count'])
    Path(pool.cache_root).mkdir(parents=True, exist_ok=True)
    ensure_free_space(pool.cache_root, pool.min_free_bytes)
    metadata = core.probe(clip['path'])
    detected, why = color.detect(metadata)
    store.record_detection(clip['id'], detected, why)
    profile, _ = color.resolve(clip['color_profile'], clip['source_profile'], detected)
    folder = (Path(pool.cache_root) / str(clip['project_id']) / clip['id'] /
              clip['fingerprint'] / color.cache_key(profile) / f'samples-{count}')
    folder.mkdir(parents=True, exist_ok=True)
    times = core.sample_times(metadata['duration'], count)
    frames, images = [], []
    for i, seconds in enumerate(times):
        checkpoint(i / len(times))
        target = folder / f'{i:03}.png'
        image = None
        if target.exists():  # preview from an interrupted run of the same source state
            try:
                image = Image.open(target).convert('RGB')
                image.load()
            except OSError:
                image = None
        if image is None:
            image = core.frame(clip['path'], seconds, metadata['stream_index'],
                               profile=profile, metadata=metadata)
            partial = folder / f'{i:03}.partial'
            image.save(partial, format='PNG')
            partial.replace(target)
        images.append(image)
        frames.append({'seconds': seconds, 'preview': str(target),
                       'metrics': core.metrics(image)})
    core.verify_source(source)
    checkpoint(1.0)
    note = None
    if profile == 'rec709' and not detected and color.looks_flat(images):
        note = ('Low contrast across all samples: possibly log footage without metadata. '
                'Set a color profile if so.')
    store.complete_analysis(clip['id'], clip['fingerprint'], metadata, f'uniform-{count}',
                            frames, profile, note)


def export_frame(store, job, pool, checkpoint):
    """Export one kept frame at full resolution. Exports are serialised so concurrent
    workers never race on the shared CSV index; decoding still overlaps with analysis."""
    checkpoint(0)
    with pool.export_lock:
        try:
            store.export_candidate(job['params']['candidate_id'], job['params']['output'])
        except FileExistsError:
            return 'Already exported; existing file was not overwritten.'
    return None


class WorkerPool:
    def __init__(self, store, cache_root, max_workers=2, poll_interval=0.5,
                 stale_after=60.0, max_attempts=3, min_free_bytes=DEFAULT_MIN_FREE_BYTES,
                 handlers=None):
        self.store = store
        self.cache_root = Path(cache_root)
        self.max_workers = max(1, min(int(max_workers), MAX_WORKERS))
        self.poll_interval = poll_interval
        self.stale_after = stale_after
        self.max_attempts = max_attempts
        self.min_free_bytes = min_free_bytes
        self.handlers = handlers or {'analyze': analyze_clip, 'export': export_frame}
        self.export_lock = threading.Lock()
        self.token = f'{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}'
        self.recovered = None
        self._stop = threading.Event()
        self._threads = []

    @property
    def running(self):
        return any(t.is_alive() for t in self._threads)

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self.recovered = self.store.recover_jobs(self.stale_after, self.max_attempts)
        self._threads = [threading.Thread(target=self._loop, args=(f'{self.token}/{i}',),
                                          name=f'framefoundry-worker-{i}', daemon=True)
                         for i in range(self.max_workers)]
        for thread in self._threads:
            thread.start()

    def stop(self, timeout=None):
        """Stop claiming work; running jobs return to the queue at their next checkpoint."""
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout)

    def run_until_idle(self, project_id=None, timeout=None):
        self.start()
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            while self.store.active_job_count(project_id):
                if deadline is not None and time.monotonic() > deadline:
                    raise TimeoutError('Jobs still active after timeout.')
                time.sleep(self.poll_interval)
        finally:
            self.stop()

    def _loop(self, worker):
        while not self._stop.is_set():
            try:
                job = self.store.claim_job(worker)
            except sqlite3.OperationalError:
                job = None  # database busy; try again shortly
            if job is None:
                self._stop.wait(self.poll_interval)
                continue
            self._execute(job)

    def _execute(self, job):
        done = threading.Event()

        def keep_lease():
            # Keeps the lease fresh during long single decodes between checkpoints.
            while not done.wait(max(self.stale_after / 3, 0.05)):
                try:
                    self.store.heartbeat(job['id'])
                except sqlite3.OperationalError:
                    pass

        def checkpoint(progress=None):
            state = self.store.heartbeat(job['id'], progress)
            if state == 'cancel_requested':
                raise Cancelled()
            if self._stop.is_set():
                raise Interrupted()

        lease = threading.Thread(target=keep_lease, daemon=True)
        lease.start()
        try:
            note = self.handlers[job['kind']](self.store, job, self, checkpoint)
        except Cancelled:
            self.store.finish_job(job['id'], 'cancelled', 'Cancelled by user.')
        except Interrupted:
            self.store.finish_job(job['id'], 'queued')
        except Skipped as reason:
            self.store.finish_job(job['id'], 'cancelled', str(reason))
        except Exception as error:  # per-clip failure must not halt the batch
            message = f'{type(error).__name__}: {error}'.strip()
            if job.get('clip_id') and job['kind'] == 'analyze':
                self.store.record_clip_error(job['clip_id'], message)
            self.store.finish_job(job['id'], 'failed', message)
        else:
            self.store.finish_job(job['id'], 'done', note if isinstance(note, str) else None)
        finally:
            done.set()
            lease.join()
