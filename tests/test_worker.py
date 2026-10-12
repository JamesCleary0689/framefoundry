"""Worker queue tests: real FFmpeg decoding on synthetic fixtures plus fake handlers
for deterministic cancellation, interruption, crash recovery and concurrency bounds."""
import hashlib
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fixtures import make_fixture
from store import Store
from worker import WorkerPool

needs_ffmpeg = unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),
                                   'FFmpeg required')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class WorkerTestCase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.store = Store(self.root / 'home' / 'ff.sqlite')
        self.cache = self.root / 'home' / 'cache'
        self.pid = self.store.create_project('Fixture Shoot', 'Fixture', ['Bars', 'Pattern'], 4)

    def tearDown(self):
        self._temp.cleanup()

    def pool(self, **kw):
        kw.setdefault('min_free_bytes', 0)
        kw.setdefault('poll_interval', 0.02)
        return WorkerPool(self.store, self.cache, **kw)

    def fake_clips(self, n):
        source = self.root / 'fake'
        for i in range(n):
            (source / f'{i}.mp4').parent.mkdir(parents=True, exist_ok=True)
            (source / f'{i}.mp4').write_bytes(b'x')
        self.store.scan_source(self.store.add_source(self.pid, source))
        self.assertEqual(self.store.enqueue_analysis(self.pid), n)


@needs_ffmpeg
class AnalysisTests(WorkerTestCase):
    def test_batch_with_corrupt_clip_export_and_integrity(self):  # A01 A02 A04 A14 A15
        cam_a, cam_b = make_fixture(self.root / 'media')
        originals = {p: digest(p) for p in (self.root / 'media').rglob('*') if p.is_file()}
        self.store.add_source(self.pid, cam_a, 'Camera A')
        self.store.add_source(self.pid, cam_b, 'Camera B')
        self.store.scan_project(self.pid)
        self.assertEqual(self.store.enqueue_analysis(self.pid), 4)
        self.pool(max_workers=2).run_until_idle(timeout=120)

        clips = {(c['source_label'], c['rel_path']): c for c in self.store.clips(self.pid)}
        corrupt = clips[('Camera B', 'DCIM/C0002.mp4')]
        self.assertTrue(corrupt['error'])
        self.assertEqual(corrupt['job_state'], 'failed')
        good = [c for c in clips.values() if c is not corrupt]
        self.assertEqual([c['job_state'] for c in good], ['done'] * 3)
        self.assertEqual([c['candidate_count'] for c in good], [4] * 3)
        self.assertEqual(clips[('Camera A', 'DCIM/C0001.mp4')]['probe']['codec'], 'mpeg4')

        # Re-enqueue adds nothing; re-running does not duplicate candidates.
        self.assertEqual(self.store.enqueue_analysis(self.pid), 0)
        self.assertEqual(self.store.resume_jobs(self.pid), 1)  # retries only the corrupt clip
        self.pool().run_until_idle(timeout=60)
        self.assertEqual(self.store.job_counts(self.pid), {'done': 3, 'failed': 1})

        # Same basename from two cameras exports to two distinct files.
        out = self.root / 'exports'
        out.mkdir()
        targets = []
        for label in ('Camera A', 'Camera B'):
            k = self.store.candidates(clips[(label, 'DCIM/C0001.mp4')]['id'])[1]
            self.store.save_selection(k['id'], decision='Keep', tag='Pattern',
                                      color_verified=True, rank='A')
            targets.append(self.store.export_candidate(k['id'], out))
        self.assertEqual(len(set(targets)), 2)
        self.assertTrue(all(t.name.startswith('Fixture_') for t in targets))
        self.assertTrue(all(t.with_suffix('.json').exists() for t in targets))
        with self.assertRaises(FileExistsError):
            self.store.export_candidate(k['id'], out)
        self.assertEqual(len(self.store.kept(self.pid)), 2)
        self.assertTrue(all(r['exported_file'] for r in self.store.kept(self.pid)))

        self.assertEqual(originals, {p: digest(p) for p in originals})

    def test_changed_source_hides_stale_candidates_and_blocks_export(self):  # A03
        cam_a, _ = make_fixture(self.root / 'media')
        self.store.scan_source(self.store.add_source(self.pid, cam_a))
        self.store.enqueue_analysis(self.pid)
        self.pool().run_until_idle(timeout=60)
        clip = [c for c in self.store.clips(self.pid) if c['rel_path'] == 'DCIM/C0001.mp4'][0]
        stale = self.store.candidates(clip['id'])[0]
        self.store.save_selection(stale['id'], decision='Keep', color_verified=True)
        from fixtures import make_clip
        make_clip(Path(clip['path']), seconds=2)
        self.store.scan_project(self.pid)
        self.assertEqual(self.store.candidates(clip['id']), [])
        self.assertEqual(self.store.kept(self.pid), [])
        with self.assertRaises(ValueError):
            self.store.export_candidate(stale['id'], self.root)
        self.assertEqual(self.store.enqueue_analysis(self.pid), 1)

    def test_interrupted_analysis_resumes_with_cached_previews(self):  # A06
        cam_a, _ = make_fixture(self.root / 'media')
        self.store.scan_source(self.store.add_source(self.pid, cam_a))
        self.store.enqueue_analysis(self.pid, sample_count=6)
        stop_after = threading.Event()
        pool = self.pool(max_workers=1)
        original = pool.handlers['analyze']

        def interrupt_midway(store, job, p, checkpoint):
            def wrapped(progress=None):
                if progress and progress >= 0.5:
                    stop_after.set()
                    p._stop.set()
                checkpoint(progress)
            original(store, job, p, wrapped)

        pool.handlers['analyze'] = interrupt_midway
        pool.start()
        self.assertTrue(stop_after.wait(60))
        pool.stop(30)
        counts = self.store.job_counts(self.pid)
        self.assertGreaterEqual(counts.get('queued', 0), 1)
        partial = list(self.cache.rglob('*.png'))
        self.assertTrue(partial)
        self.pool().run_until_idle(timeout=60)
        self.assertEqual(self.store.job_counts(self.pid), {'done': 2})
        for clip in self.store.clips(self.pid):
            self.assertEqual(len(self.store.candidates(clip['id'])), 6)


@needs_ffmpeg
class ExportJobTests(WorkerTestCase):
    def test_mass_export_in_background_with_per_frame_failures(self):  # A04 A14 A15
        cam_a, cam_b = make_fixture(self.root / 'media')
        originals = {p: digest(p) for p in (self.root / 'media').rglob('*') if p.is_file()}
        self.store.add_source(self.pid, cam_a, 'Camera A')
        self.store.add_source(self.pid, cam_b, 'Camera B')
        self.store.scan_project(self.pid)
        self.store.enqueue_analysis(self.pid)
        self.pool(max_workers=3).run_until_idle(timeout=120)
        kept = []
        for clip in self.store.clips(self.pid):
            for k in self.store.candidates(clip['id'])[:2]:
                self.store.save_selection(k['id'], decision='Keep', color_verified=True)
                kept.append(k['id'])
        self.store.save_selection(kept[-1], color_verified=False)  # will fail: not checked
        out = self.root / 'exports'
        out.mkdir()
        self.assertEqual(self.store.enqueue_exports(self.pid, kept, out), 6)
        self.assertEqual(self.store.enqueue_exports(self.pid, kept, out), 0)  # no duplicates
        with self.assertRaises(FileNotFoundError):
            self.store.enqueue_exports(self.pid, kept, self.root / 'unmounted')
        self.pool(max_workers=3).run_until_idle(timeout=120)
        counts = self.store.job_counts_by_kind(self.pid, 'export')
        self.assertEqual(counts, {'done': 5, 'failed': 1})
        folder = out / 'Fixture_Still_Selects'
        self.assertEqual(len(list(folder.glob('*.png'))), 5)
        self.assertEqual(len(list(folder.glob('*.json'))), 5)
        index = (folder / 'stills_index.csv').read_text().splitlines()
        self.assertEqual(len(index), 6)  # header + 5 rows, written without races
        failed = [j for j in self.store.jobs(self.pid) if j['kind'] == 'export'
                  and j['state'] == 'failed'][0]
        self.assertIn('Confirm display color', failed['error'])
        # Analysis status of clips is unaffected by export jobs.
        self.assertEqual(sorted(c['job_state'] for c in self.store.clips(self.pid)),
                         ['done', 'done', 'done', 'failed'])  # failed = the corrupt clip

        # Fix the cause and retry only the failed frame; re-exporting is never an overwrite.
        self.store.save_selection(kept[-1], color_verified=True)
        self.assertEqual(self.store.resume_jobs(self.pid), 2)  # the export + corrupt clip
        self.assertEqual(self.store.enqueue_exports(self.pid, kept[:1], out), 1)
        self.pool().run_until_idle(timeout=60)
        self.assertEqual(len(list(folder.glob('*.png'))), 6)
        notes = [j['error'] for j in self.store.jobs(self.pid)
                 if j['kind'] == 'export' and j['state'] == 'done' and j['error']]
        self.assertEqual(notes, ['Already exported; existing file was not overwritten.'])
        self.assertEqual(originals, {p: digest(p) for p in originals})


class QueueBehaviourTests(WorkerTestCase):
    def test_concurrency_is_bounded(self):
        self.fake_clips(8)
        lock, active, peak = threading.Lock(), [0], [0]

        def handler(store, job, pool, checkpoint):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            checkpoint(1)
            with lock:
                active[0] -= 1

        self.pool(max_workers=3, handlers={'analyze': handler}).run_until_idle(timeout=30)
        self.assertEqual(self.store.job_counts(self.pid), {'done': 8})
        self.assertLessEqual(peak[0], 3)
        self.assertGreaterEqual(peak[0], 2)
        self.assertEqual(self.pool(max_workers=99).max_workers, 8)

    def test_cancel_running_job_at_checkpoint(self):  # A06
        self.fake_clips(1)
        started = threading.Event()

        def handler(store, job, pool, checkpoint):
            started.set()
            for _ in range(500):
                checkpoint()
                time.sleep(0.01)

        pool = self.pool(handlers={'analyze': handler})
        pool.start()
        self.assertTrue(started.wait(10))
        self.store.cancel_jobs(self.pid)
        deadline = time.time() + 10
        while self.store.active_job_count(self.pid) and time.time() < deadline:
            time.sleep(0.02)
        pool.stop()
        job = self.store.jobs(self.pid)[0]
        self.assertEqual((job['state'], job['error']), ('cancelled', 'Cancelled by user.'))

    def test_crash_recovery_requeues_orphans_once(self):  # A06
        self.fake_clips(2)
        orphan = self.store.claim_job('dead-process/0')
        with self.store.tx() as db:
            db.execute('UPDATE jobs SET heartbeat=0 WHERE id=?', (orphan['id'],))
        ran = []
        pool = self.pool(stale_after=5,
                         handlers={'analyze': lambda s, j, p, c: ran.append(j['id'])})
        pool.run_until_idle(timeout=30)
        self.assertEqual(pool.recovered['requeued'], 1)
        self.assertEqual(sorted(ran), sorted(j['id'] for j in self.store.jobs(self.pid)))
        self.assertEqual(self.store.job_counts(self.pid), {'done': 2})

    def test_live_lease_is_not_stolen(self):
        self.fake_clips(1)
        release = threading.Event()

        def slow(store, job, pool, checkpoint):
            release.wait(5)  # one long "decode" with no checkpoint

        first = self.pool(stale_after=0.3, handlers={'analyze': slow})
        first.start()
        time.sleep(0.8)
        self.assertEqual(self.store.recover_jobs(stale_after=0.3)['requeued'], 0)
        release.set()
        first.stop(5)
        self.assertEqual(self.store.job_counts(self.pid), {'done': 1})

    def test_handler_exception_marks_failed_and_continues(self):  # A04
        self.fake_clips(3)

        def flaky(store, job, pool, checkpoint):
            if job['id'] == 2:
                raise RuntimeError('decoder exploded')

        self.pool(handlers={'analyze': flaky}).run_until_idle(timeout=30)
        self.assertEqual(self.store.job_counts(self.pid), {'done': 2, 'failed': 1})
        errors = [c['error'] for c in self.store.clips(self.pid) if c['error']]
        self.assertEqual(errors, ['RuntimeError: decoder exploded'])

    def test_low_disk_space_fails_cleanly(self):  # A05
        self.fake_clips(1)
        self.pool(min_free_bytes=1 << 62).run_until_idle(timeout=30)
        job = self.store.jobs(self.pid)[0]
        self.assertEqual(job['state'], 'failed')
        self.assertIn('Low disk space', job['error'])


if __name__ == '__main__':
    unittest.main()
