"""Technical score, near-duplicate suppression, quick actions and schema migration."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

import core
import scoring
import store as store_module
from store import Store


def pattern(seed, size=(320, 180)):
    """A distinct, detailed test image (different seeds give visibly different scenes)."""
    img = Image.new('RGB', size, (90, 90, 90))
    draw = ImageDraw.Draw(img)
    for i in range(0, size[0], 8 + seed % 7):
        draw.line([(i, 0), ((i * (seed + 3)) % size[0], size[1])], fill=(230, 40 * seed % 255, 30))
    draw.ellipse([20 * seed % 200, 30, 20 * seed % 200 + 80, 150], fill=(20, 200, 240))
    return img


class ScoringCase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()

    def candidate(self, name, image, seconds):
        path = self.root / f'{name}.png'
        image.save(path)
        return {'id': name, 'seconds': seconds, 'preview': str(path),
                'metrics': core.metrics(image), 'decision': 'Review'}


class ScoreTests(ScoringCase):
    def test_percentile_ranks_handle_ties_and_single(self):
        self.assertEqual(scoring.percentile_ranks([5]), [1.0])
        self.assertEqual(scoring.percentile_ranks([1, 2, 2, 3]), [0.0, 0.5, 0.5, 1.0])

    def test_sharp_beats_blurred_and_reasons_explain(self):
        sharp = pattern(1)
        cands = [self.candidate('sharp', sharp, 1),
                 self.candidate('soft', sharp.filter(ImageFilter.GaussianBlur(6)), 2)]
        by = {k['id']: k for k in scoring.score_candidates(cands, 1)}
        self.assertGreater(by['sharp']['score'], by['soft']['score'])
        self.assertEqual(by['sharp']['suggested_rank'], 1)
        self.assertIn('among the sharpest in this clip', by['sharp']['reasons'])
        self.assertIn('softer than most of this clip', by['soft']['reasons'])

    def test_clipping_penalised_but_darkness_tolerated(self):  # A09
        base = pattern(2)
        blown = base.copy()
        ImageDraw.Draw(blown).rectangle([0, 0, 320, 60], fill=(255, 255, 255))
        dark = base.copy()
        ImageDraw.Draw(dark).rectangle([0, 0, 320, 36], fill=(0, 0, 0))  # 20% black: tolerated
        self.assertLess(scoring.exposure_quality(core.metrics(blown)), 0.5)
        self.assertEqual(scoring.exposure_quality(core.metrics(dark)), 1.0)
        night = base.copy()
        ImageDraw.Draw(night).rectangle([0, 0, 320, 120], fill=(0, 0, 0))
        k = scoring.score_candidates([self.candidate('night', night, 1)])[0]
        self.assertTrue(any('may be intentional' in r for r in k['reasons']))
        b = scoring.score_candidates([self.candidate('blown', blown, 1)])[0]
        self.assertTrue(any('clipped highlights' in r for r in b['reasons']))

    def test_near_duplicates_suppressed(self):  # A12
        cands = [self.candidate('a', pattern(1), 1), self.candidate('a2', pattern(1), 2),
                 self.candidate('b', pattern(4), 3), self.candidate('c', pattern(6), 4)]
        scored = scoring.score_candidates(cands, 5)
        picks = scoring.suggestions(scored)
        self.assertEqual(len(picks), 3)
        dup = [k for k in scored if k['duplicate_of']]
        self.assertEqual(len(dup), 1)
        self.assertIn(dup[0]['id'], ('a', 'a2'))
        self.assertTrue(any('near-duplicate' in r for r in dup[0]['reasons']))
        self.assertEqual([k['suggested_rank'] for k in picks], [1, 2, 3])

    def test_unreadable_preview_is_not_a_duplicate(self):
        cands = [self.candidate('a', pattern(1), 1), self.candidate('b', pattern(1), 2)]
        Path(cands[1]['preview']).unlink()
        self.assertEqual(len(scoring.suggestions(scoring.score_candidates(cands, 5))), 2)


class QuickActionTests(ScoringCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root / 'db.sqlite')
        self.pid = self.store.create_project('Auto', 'Auto')
        self.clips = []
        for cam in ('A', 'B'):
            (self.root / cam).mkdir()
            (self.root / cam / 'clip.mp4').write_bytes(b'x')
            self.store.scan_source(self.store.add_source(self.pid, self.root / cam, cam))
        for i, clip in enumerate(self.store.clips(self.pid)):
            frames = []
            for j in range(6):
                img = pattern(j + 1 + i * 7)
                if j == 5:
                    img = img.filter(ImageFilter.GaussianBlur(8))
                path = self.root / f'{clip["id"]}-{j}.png'
                img.save(path)
                frames.append({'seconds': j + 0.5, 'preview': str(path),
                               'metrics': core.metrics(img)})
            self.store.complete_analysis(clip['id'], clip['fingerprint'], {'duration': 6},
                                         'uniform-6', frames)
            self.clips.append(clip)

    def test_autopick_labels_auto_and_skips_clips_a_person_started(self):
        human_clip, auto_clip = self.clips
        first = self.store.candidates(human_clip['id'])[0]
        self.store.save_selection(first['id'], decision='Reject')
        changed = scoring.autopick(self.store, self.pid, top_n=3, reject_rest=True)
        self.assertEqual(list(changed), [auto_clip['id']])
        cands = self.store.candidates(auto_clip['id'])
        self.assertEqual(sum(k['decision'] == 'Keep' for k in cands), 3)
        self.assertEqual(sum(k['decision'] == 'Reject' for k in cands), 3)
        self.assertTrue(all(k['decided_by'] == 'auto' for k in cands))
        blurred = [k for k in cands if k['seconds'] == 5.5][0]
        self.assertEqual(blurred['decision'], 'Reject')
        untouched = self.store.candidates(human_clip['id'])
        self.assertEqual([k['decision'] for k in untouched], ['Reject'] + ['Review'] * 5)
        # Re-running does nothing new.
        self.assertEqual(scoring.autopick(self.store, self.pid, 3, True), {auto_clip['id']: 0})

    def test_bulk_never_overrides_human_and_undo_restores(self):
        clip = self.clips[0]
        ids = [k['id'] for k in self.store.candidates(clip['id'])]
        self.store.save_selection(ids[0], decision='Reject')        # human
        self.assertEqual(self.store.bulk_decide(ids[:3], 'Keep'), 2)
        self.assertEqual(self.store.bulk_decide(ids[:3], 'Reject'), 2)  # auto may change auto
        self.assertEqual(self.store.bulk_decide(ids[:3], 'Keep', overwrite_auto=False), 0)
        self.store.save_selection(ids[1], decision='Keep')          # person confirms one
        self.assertEqual(self.store.undo_auto(clip_id=clip['id']), 1)
        state = {k['id']: (k['decision'], k['decided_by'])
                 for k in self.store.candidates(clip['id'])}
        self.assertEqual(state[ids[0]], ('Reject', 'human'))
        self.assertEqual(state[ids[1]], ('Keep', 'human'))
        self.assertEqual(state[ids[2]][0], 'Review')
        self.assertEqual(self.store.mark_color_verified(ids[:2]), 2)
        self.assertEqual(self.store.mark_color_verified(ids[:2]), 0)
        progress = self.store.clip_progress(self.pid)[clip['id']]
        self.assertEqual(progress, {'Review': 4, 'Keep': 1, 'Reject': 1})


class MigrationTests(unittest.TestCase):
    def test_version_1_database_upgrades_in_place(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'old.sqlite'
            db = sqlite3.connect(path)
            for statement in store_module.SCHEMA.split(';'):
                if statement.strip():
                    db.execute(statement)
            db.execute("INSERT INTO meta VALUES('schema_version', '1')")
            db.execute("INSERT INTO projects(name, slug, export_prefix, tags, created_at, "
                       "updated_at) VALUES('Old', 'old', 'Old', '[]', 'x', 'x')")
            db.commit()
            db.close()
            s = Store(path)
            self.assertEqual(s.one("SELECT value FROM meta WHERE key='schema_version'")['value'],
                             str(store_module.SCHEMA_VERSION))
            cols = [r['name'] for r in s.query('PRAGMA table_info(selections)')]
            self.assertIn('decided_by', cols)
            self.assertEqual(s.project_by_slug('old')['name'], 'Old')
            Store(path)  # reopening an upgraded database is a no-op

    def test_newer_database_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            s = Store(Path(temp) / 'db.sqlite')
            with s.tx() as db:
                db.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
            with self.assertRaises(RuntimeError):
                Store(Path(temp) / 'db.sqlite')


if __name__ == '__main__':
    unittest.main()
