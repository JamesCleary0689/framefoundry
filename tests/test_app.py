"""Headless Streamlit smoke test of the review UI against a synthetic fixture project."""
import gc
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fixtures import make_fixture
from store import Store
from worker import WorkerPool

try:
    from streamlit.testing.v1 import AppTest
except ImportError:  # pragma: no cover
    AppTest = None

APP = str(Path(__file__).resolve().parent.parent / 'app.py')


@unittest.skipUnless(AppTest and shutil.which('ffmpeg'), 'Streamlit and FFmpeg required')
class AppSmokeTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.home = Path(self._temp.name) / 'home'
        self.env = mock.patch.dict(os.environ, {'FRAMEFOUNDRY_HOME': str(self.home),
                                                'FRAMEFOUNDRY_MIN_FREE_MB': '0'})
        self.env.start()
        store = Store(self.home / 'framefoundry.sqlite')
        cam_a, cam_b = make_fixture(Path(self._temp.name) / 'media')
        self.pid = store.create_project('UI Fixture', 'UIFix', ['Bars'], 4)
        store.add_source(self.pid, cam_a, 'Camera A')
        store.add_source(self.pid, cam_b, 'Camera B')
        store.scan_project(self.pid)
        store.enqueue_analysis(self.pid)
        WorkerPool(store, self.home / 'cache', min_free_bytes=0,
                   poll_interval=0.02).run_until_idle(timeout=120)
        self.store = store

    def tearDown(self):
        # Stop the app's background workers before deleting their database folder.
        for obj in gc.get_objects():
            if isinstance(obj, WorkerPool) and str(obj.cache_root).startswith(self._temp.name):
                obj.stop(10)
        self.env.stop()
        self._temp.cleanup()

    def test_review_flow_persists(self):
        at = AppTest.from_file(APP, default_timeout=60).run()
        self.assertFalse(at.exception, at.exception)
        self.assertEqual([t.label for t in at.tabs],
                         ['Sources', 'Clips', 'Queue', 'Contact sheet', 'Selections & export'])
        # Contact sheet shows one decision control per candidate of the first analyzed clip.
        groups = [g for g in at.button_group if str(g.key).startswith('decision-')]
        self.assertEqual(len(groups), 4)
        groups[0].set_value('Keep').run()
        self.assertFalse(at.exception, at.exception)
        self.assertEqual(len(self.store.kept(self.pid)), 1)

        # A persisted exclusion hides the clip from the contact sheet (AppTest cannot drive data_editor).
        clips = self.store.clips(self.pid)
        first = [c for c in clips if c['candidate_count']][0]
        self.store.set_excluded(first['id'], True, 'test')
        at = AppTest.from_file(APP, default_timeout=60).run()
        self.assertFalse(at.exception, at.exception)
        clip_picker = [s for s in at.selectbox if s.label == 'Clip'][0]
        label = f"{first['source_label']} / {first['rel_path']} —"
        self.assertFalse(any(label in o for o in clip_picker.options), clip_picker.options)
        self.assertEqual(len(clip_picker.options), 2)

    def test_browse_fills_source_path_and_adds_source(self):
        import picker
        chosen = Path(self._temp.name) / 'Picked Card'
        chosen.mkdir()
        with mock.patch.object(picker, 'available', return_value=True), \
                mock.patch.object(picker, 'pick_folder', return_value=str(chosen)) as pick:
            at = AppTest.from_file(APP, default_timeout=60).run()
            at.button(key='source-path-browse').click().run()
            self.assertFalse(at.exception, at.exception)
            self.assertEqual(pick.call_args.args[0],
                             'Choose a folder of video clips (e.g. one camera or card)')
            self.assertEqual(at.text_input(key='source-path').value, str(chosen))
            at.text_input(key='source-label').input('Camera C')
            [b for b in at.button if b.label == 'Add source'][0].click().run()
            self.assertFalse(at.exception, at.exception)
        labels = [s['label'] for s in self.store.sources(self.pid)]
        self.assertIn('Camera C', labels)
        self.assertEqual(at.text_input(key='source-path').value, '')

    def test_quick_actions_and_export_all(self):
        at = AppTest.from_file(APP, default_timeout=60).run()
        keep = [b for b in at.button if b.label.startswith('✓ Keep top ') and 'reject' not in b.label]
        keep[0].click().run()
        self.assertFalse(at.exception, at.exception)
        kept = self.store.kept(self.pid)
        self.assertGreaterEqual(len(kept), 1)
        self.assertTrue(all(r['decided_by'] == 'auto' for r in kept))
        self.assertIn('★ #1', ' '.join(m.value for m in at.markdown))

        [b for b in at.button if b.label == 'Color checked for kept'][0].click().run()
        self.assertTrue(all(r['color_verified'] for r in self.store.kept(self.pid)))

        out = Path(self._temp.name) / 'exports'
        out.mkdir()
        pid = self.pid
        at.text_input(key=f'output-{pid}').input(str(out)).run()
        self.assertFalse([c for c in at.checkbox if c.key == f'attest-{pid}'])  # all checked
        [b for b in at.button if b.label.startswith('Export all ')][0].click().run()
        self.assertFalse(at.exception, at.exception)
        deadline = time.time() + 60  # exports run on the app's background workers
        while len(list(out.rglob('*.png'))) < len(kept) and time.time() < deadline:
            time.sleep(0.2)
        pngs = list(out.rglob('*.png'))
        self.assertEqual(len(pngs), len(kept))
        sidecar = pngs[0].with_suffix('.json').read_text()
        self.assertIn('"decided_by": "auto"', sidecar)

        [b for b in at.button if b.label == '↺ Undo auto-picks'][0].click().run()
        self.assertEqual(self.store.kept(self.pid), [])

    def test_back_next_and_next_to_review(self):
        at = AppTest.from_file(APP, default_timeout=60).run()

        def position():
            return [c.value for c in at.caption if c.value.startswith('Clip ')][0]

        order = at.selectbox(key='sheet-clip').options
        self.assertEqual(len(order), 3)
        self.assertEqual(position(), 'Clip 1 of 3')
        self.assertTrue(at.button(key='back-top').disabled)
        # Keeping a frame then pressing Next moves forward, never back to the start.
        [b for b in at.button if b.label == '✓ Keep #1 only'][0].click().run()
        at.button(key='next-top').click().run()
        self.assertEqual(position(), 'Clip 2 of 3')
        [b for b in at.button if b.label == '✓ Keep #1 only'][0].click().run()
        at.button(key='next-bottom').click().run()
        self.assertEqual(position(), 'Clip 3 of 3')
        self.assertTrue(at.button(key='next-top').disabled)
        at.button(key='back-bottom').click().run()
        self.assertEqual(position(), 'Clip 2 of 3')
        self.assertFalse(at.exception, at.exception)

        # Next to review skips a clip with nothing left in Review.
        clip2 = [c for c in self.store.clips(self.pid) if c['candidate_count']][1]
        self.store.bulk_decide([k['id'] for k in self.store.candidates(clip2['id'])], 'Reject',
                               decided_by='human')
        at.button(key='back-top').click().run()
        self.assertEqual(position(), 'Clip 1 of 3')
        at.button(key='review-top').click().run()
        self.assertEqual(position(), 'Clip 3 of 3')

    def test_mass_export_requires_color_confirmation(self):
        at = AppTest.from_file(APP, default_timeout=60).run()
        [b for b in at.button if b.label == '✓ Keep top, ✕ reject rest'][0].click().run()
        out = Path(self._temp.name) / 'exports'
        out.mkdir()
        at.text_input(key=f'output-{self.pid}').input(str(out)).run()
        export_all = lambda: [b for b in at.button if b.label.startswith('Export all ')][0]
        export_all().click().run()
        self.assertTrue(any('not been color-checked' in e.value for e in at.error))
        self.assertEqual(self.store.job_counts_by_kind(self.pid, 'export'), {})
        at.checkbox(key=f'attest-{self.pid}').check().run()
        export_all().click().run()
        self.assertTrue(any('Queued' in s.value for s in at.success))
        self.assertTrue(all(r['color_verified'] for r in self.store.kept(self.pid)))

    def test_clips_tab_hides_analyzed_clips_by_default(self):
        at = AppTest.from_file(APP, default_timeout=60).run()
        self.assertFalse(at.exception, at.exception)
        captions = ' '.join(c.value for c in at.caption)
        self.assertIn('3 analyzed clip(s) hidden', captions)  # the corrupt clip stays listed
        editor = [d for d in at.dataframe if 'clip' in d.value.columns][0]
        self.assertEqual(list(editor.value['clip']), ['DCIM/C0002.mp4'])
        at.toggle(key=f'show-analyzed-{self.pid}').set_value(True).run()
        editor = [d for d in at.dataframe if 'clip' in d.value.columns][0]
        self.assertEqual(len(editor.value), 4)
