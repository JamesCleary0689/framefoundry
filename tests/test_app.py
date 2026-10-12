"""Headless Streamlit smoke test of the review UI against a synthetic fixture project."""
import os
import shutil
import tempfile
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
        label = f"{first['source_label']} / {first['rel_path']}"
        self.assertNotIn(label, clip_picker.options)
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
