"""SQLite project model tests. These do not need FFmpeg: inventory only stats files."""
import os
import tempfile
import time
import unittest
from pathlib import Path

from store import Store


def touch(path, data=b'x'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.db = self.root / 'data' / 'ff.sqlite'
        self.store = Store(self.db)
        self.pid = self.store.create_project('Test Shoot', 'TS', ['Wide', 'Close'], 4)

    def tearDown(self):
        self._temp.cleanup()

    def add_clip_job(self):
        touch(self.root / 'A' / 'a.mp4')
        sid = self.store.add_source(self.pid, self.root / 'A')
        self.store.scan_source(sid)
        self.store.enqueue_analysis(self.pid)
        return self.store.clips(self.pid)[0]


class ProjectTests(StoreTestCase):
    def test_configurable_tags_prefix_and_reopen(self):  # A16, A19
        p = self.store.project(self.pid)
        self.assertEqual(p['tags'], ['Wide', 'Close', 'Other'])
        self.assertEqual(p['export_prefix'], 'TS')
        self.store.update_project(self.pid, export_prefix='New Prefix/..', tags=['X'],
                                  sample_count=8)
        reopened = Store(self.db).project_by_slug('test-shoot')
        self.assertEqual(reopened['export_prefix'], 'New-Prefix')
        self.assertEqual(reopened['tags'], ['X', 'Other'])
        self.assertEqual(reopened['sample_count'], 8)

    def test_default_tags_are_generic(self):  # A16
        p = self.store.project(self.store.create_project('Other Job'))
        self.assertNotIn('PumpkinPatch', p['tags'])
        self.assertEqual(p['export_prefix'], 'Other-Job')

    def test_invalid_project_values_rejected(self):
        with self.assertRaises(ValueError):
            self.store.create_project('  ')
        with self.assertRaises(Exception):
            self.store.create_project('Bad', sample_count=99)


class InventoryTests(StoreTestCase):
    def test_multi_source_duplicate_basenames_stay_distinct(self):  # A02
        touch(self.root / 'Cam A' / 'DCIM' / 'C0001.MP4')
        touch(self.root / 'Cam B' / 'DCIM' / 'C0001.MP4')
        touch(self.root / 'Cam B' / 'readme.txt')
        a = self.store.add_source(self.pid, self.root / 'Cam A', 'Camera A')
        b = self.store.add_source(self.pid, self.root / 'Cam B', 'Camera B')
        self.store.scan_project(self.pid)
        clips = self.store.clips(self.pid)
        self.assertEqual(len(clips), 2)
        self.assertEqual({c['source_label'] for c in clips}, {'Camera A', 'Camera B'})
        self.assertEqual({c['rel_path'] for c in clips}, {'DCIM/C0001.MP4'})
        self.assertEqual(len({c['id'] for c in clips}), 2)
        self.assertEqual(len({c['fingerprint'] for c in clips}), 2)
        self.assertNotEqual(a, b)

    def test_overlapping_and_missing_sources_rejected(self):
        (self.root / 'Top' / 'Sub').mkdir(parents=True)
        self.store.add_source(self.pid, self.root / 'Top')
        with self.assertRaises(ValueError):
            self.store.add_source(self.pid, self.root / 'Top' / 'Sub')
        with self.assertRaises(FileNotFoundError):
            self.store.add_source(self.pid, self.root / 'nope')

    def test_rescan_detects_change_missing_and_return(self):  # A03
        clip = touch(self.root / 'S' / 'a.mov')
        gone = touch(self.root / 'S' / 'b.mov')
        sid = self.store.add_source(self.pid, self.root / 'S')
        self.assertEqual(self.store.scan_source(sid)['added'], 2)
        self.assertEqual(self.store.scan_source(sid)['unchanged'], 2)
        before = {c['rel_path']: c for c in self.store.clips(self.pid)}
        touch(clip, b'longer content')
        os.utime(clip, ns=(time.time_ns(), time.time_ns() + 10 ** 9))
        gone.unlink()
        counts = self.store.scan_source(sid)
        self.assertEqual((counts['changed'], counts['missing']), (1, 1))
        after = {c['rel_path']: c for c in self.store.clips(self.pid)}
        self.assertEqual(after['a.mov']['id'], before['a.mov']['id'])
        self.assertNotEqual(after['a.mov']['fingerprint'], before['a.mov']['fingerprint'])
        self.assertEqual(after['b.mov']['status'], 'missing')
        touch(gone)
        self.store.scan_source(sid)
        self.assertEqual(self.store.clip(before['b.mov']['id'])['status'], 'present')

    def test_offline_source_keeps_clips_and_decisions(self):  # A05
        touch(self.root / 'Drive' / 'a.mp4')
        sid = self.store.add_source(self.pid, self.root / 'Drive')
        self.store.scan_source(sid)
        clip = self.store.clips(self.pid)[0]
        self.store.set_excluded(clip['id'], True, 'interview')
        (self.root / 'Drive').rename(self.root / 'Unmounted')
        result = self.store.scan_source(sid)
        self.assertTrue(result['offline'])
        self.assertEqual(self.store.sources(self.pid)[0]['status'], 'offline')
        kept = self.store.clip(clip['id'])
        self.assertEqual(kept['status'], 'present')
        self.assertTrue(kept['excluded'])
        (self.root / 'Unmounted').rename(self.root / 'Drive')
        self.assertFalse(self.store.scan_source(sid)['offline'])
        self.assertEqual(self.store.sources(self.pid)[0]['status'], 'online')

    def test_exclusions_reversible_with_history(self):  # A11
        touch(self.root / 'S' / 'a.mp4')
        self.store.scan_source(self.store.add_source(self.pid, self.root / 'S'))
        cid = self.store.clips(self.pid)[0]['id']
        self.assertTrue(self.store.set_excluded(cid, True, 'talking head'))
        self.assertFalse(self.store.set_excluded(cid, True, 'talking head'))
        self.assertEqual(self.store.clips(self.pid, include_excluded=False), [])
        self.assertTrue(self.store.set_excluded(cid, False))
        self.assertFalse(Store(self.db).clip(cid)['excluded'])
        history = self.store.exclusion_history(cid)
        self.assertEqual([(h['excluded'], h['reason']) for h in history],
                         [(1, 'talking head'), (0, '')])


class SelectionTests(StoreTestCase):
    def test_selections_upsert_and_survive_reanalysis(self):  # A19
        clip = self.add_clip_job()
        frames = [{'seconds': s, 'preview': f'/p/{s}.png', 'metrics': {'edge_variance': 1}}
                  for s in (0.25, 0.75)]
        self.store.complete_analysis(clip['id'], clip['fingerprint'], {'duration': 1},
                                     'uniform-2', frames)
        first = self.store.candidates(clip['id'])[0]
        self.assertEqual(first['decision'], 'Review')
        self.store.save_selection(first['id'], decision='Keep', tag='Wide')
        self.store.save_selection(first['id'], notes='sharp')
        self.store.complete_analysis(clip['id'], clip['fingerprint'], {'duration': 1},
                                     'uniform-2', frames)
        again = Store(self.db).candidates(clip['id'])
        self.assertEqual(len(again), 2)
        self.assertEqual((again[0]['decision'], again[0]['tag'], again[0]['notes']),
                         ('Keep', 'Wide', 'sharp'))
        self.assertEqual(len(self.store.kept(self.pid)), 1)
        with self.assertRaises(ValueError):
            self.store.save_selection(first['id'], decision='Maybe')
        with self.assertRaises(ValueError):
            self.store.save_selection(first['id'], score=5)


class JobTests(StoreTestCase):
    def test_enqueue_is_idempotent_and_skips_excluded(self):  # A06, A11
        clip = self.add_clip_job()
        self.assertEqual(self.store.enqueue_analysis(self.pid), 0)
        self.assertEqual(self.store.job_counts(self.pid), {'queued': 1})
        self.store.cancel_jobs(self.pid)
        self.store.set_excluded(clip['id'], True)
        self.assertEqual(self.store.enqueue_analysis(self.pid), 0)
        self.assertEqual(self.store.resume_jobs(self.pid), 0)

    def test_cancel_and_resume_reuse_same_row(self):  # A06
        self.add_clip_job()
        job = self.store.claim_job('w1')
        self.assertEqual(job['state'], 'running')
        self.assertIsNone(self.store.claim_job('w2'))
        self.assertEqual(self.store.cancel_jobs(self.pid), 1)
        self.assertEqual(self.store.heartbeat(job['id']), 'cancel_requested')
        self.store.finish_job(job['id'], 'cancelled')
        self.assertEqual(self.store.resume_jobs(self.pid), 1)
        self.assertEqual(self.store.resume_jobs(self.pid), 0)
        self.assertEqual(self.store.enqueue_analysis(self.pid), 0)
        jobs = self.store.jobs(self.pid)
        self.assertEqual([(j['id'], j['state']) for j in jobs], [(job['id'], 'queued')])

    def test_recover_stale_jobs_and_poison_limit(self):  # A06
        self.add_clip_job()
        for attempt in (1, 2):
            job = self.store.claim_job('crashed-worker')
            self.assertEqual(job['attempts'], attempt)
            self.assertEqual(self.store.recover_jobs(stale_after=3600)['requeued'], 0)
            self.assertEqual(self.store.recover_jobs(stale_after=-1)['requeued'], 1)
        self.store.claim_job('crashed-worker')
        counts = self.store.recover_jobs(stale_after=-1, max_attempts=3)
        self.assertEqual(counts['failed'], 1)
        job = self.store.jobs(self.pid)[0]
        self.assertEqual(job['state'], 'failed')
        self.assertIn('Interrupted 3 times', job['error'])
        self.assertEqual(len(self.store.jobs(self.pid)), 1)

    def test_failed_clip_not_reenqueued_until_source_changes(self):  # A04, A06
        clip = self.add_clip_job()
        job = self.store.claim_job('w')
        self.store.finish_job(job['id'], 'failed', 'corrupt')
        self.assertEqual(self.store.enqueue_analysis(self.pid), 0)
        touch(Path(clip['path']), b'replaced with a fixed file')
        self.store.scan_project(self.pid)
        self.assertEqual(self.store.enqueue_analysis(self.pid), 1)

    def test_cancel_queued_is_immediate(self):
        self.add_clip_job()
        self.assertEqual(self.store.cancel_jobs(self.pid), 1)
        self.assertEqual(self.store.job_counts(self.pid), {'cancelled': 1})
        self.assertIsNone(self.store.claim_job('w'))


if __name__ == '__main__':
    unittest.main()
