"""Color profiles: published curves, gamut matrices, LUTs, detection and the decode
pipeline (YUV matrix, log -> Rec.709 previews, provenance)."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

import color
import core
from store import Store
from worker import WorkerPool

needs_ffmpeg = unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),
                                   'FFmpeg required')


class CurveTests(unittest.TestCase):
    def test_published_mid_grey_code_values_decode_to_18_percent(self):
        self.assertAlmostEqual(float(color.vlog_to_linear(np.array(0.423))), 0.18, delta=0.003)
        self.assertAlmostEqual(float(color.slog3_to_linear(np.array(420 / 1023))), 0.18,
                               places=4)
        self.assertAlmostEqual(float(color.logc3_to_linear(np.array(0.391))), 0.18, delta=0.002)
        self.assertAlmostEqual(float(color.hlg_to_linear(np.array(0.38))), 0.18, places=6)
        self.assertAlmostEqual(float(color.pq_to_linear(np.array(0.58))), 1.0, delta=0.03)

    def test_curves_are_monotonic(self):
        x = np.linspace(0, 1, 2001)
        for f in (color.vlog_to_linear, color.slog3_to_linear, color.logc3_to_linear,
                  color.hlg_to_linear, color.pq_to_linear, color.rec709_oetf, color.roll_off):
            self.assertTrue(np.all(np.diff(f(x)) >= -1e-9), f.__name__)

    def test_gamut_matrices_preserve_white_and_identity(self):
        np.testing.assert_allclose(color.gamut_matrix(color.REC709), np.eye(3), atol=1e-9)
        for primaries in (color.V_GAMUT, color.S_GAMUT3, color.S_GAMUT3_CINE, color.AWG3,
                          color.BT2020):
            np.testing.assert_allclose(color.gamut_matrix(primaries).sum(axis=1), 1, atol=1e-9)
        # Published BT.2020 -> BT.709 matrix (ITU-R BT.2087), first row.
        np.testing.assert_allclose(color.gamut_matrix(color.BT2020)[0],
                                   [1.6605, -0.5876, -0.0728], atol=2e-4)

    def test_roll_off_keeps_midtones_and_compresses_highlights(self):
        self.assertAlmostEqual(float(color.roll_off(np.array(0.18))), 0.18)
        self.assertLess(float(color.roll_off(np.array(8.0))), 1.0)
        self.assertGreater(float(color.roll_off(np.array(8.0))), 0.99)

    def test_log_grey_card_lands_near_rec709_grey(self):
        for profile, grey in (('vlog', 0.423), ('slog3-cine', 420 / 1023), ('logc3', 0.391)):
            full = (grey * 1023 - 64) / 876  # code value as decoded from limited range
            out = color.to_rec709(np.full((1, 1, 3), full, np.float32), profile)
            self.assertAlmostEqual(float(out[0, 0, 0]), float(color.rec709_oetf(0.18)),
                                   delta=0.01, msg=profile)
            self.assertAlmostEqual(float(out[0, 0, 0]), float(out[0, 0, 2]), delta=1e-4)

    def test_rec709_profile_is_identity(self):
        rgb = np.random.default_rng(1).random((4, 4, 3)).astype(np.float32)
        self.assertIs(color.to_rec709(rgb, 'rec709'), rgb)


class LutTests(unittest.TestCase):
    def write_cube(self, path, size, fn):
        lines = ['TITLE "test"', f'LUT_3D_SIZE {size}']
        for b in range(size):
            for g in range(size):
                for r in range(size):
                    lines.append(' '.join(f'{v:.6f}' for v in fn(r / (size - 1), g / (size - 1),
                                                                 b / (size - 1))))
        path.write_text('\n'.join(lines))

    def test_identity_and_channel_swap_luts(self):
        with tempfile.TemporaryDirectory() as temp:
            ident, swap = Path(temp) / 'id.cube', Path(temp) / 'swap.cube'
            self.write_cube(ident, 17, lambda r, g, b: (r, g, b))
            self.write_cube(swap, 9, lambda r, g, b: (b, g, r))
            rgb = np.random.default_rng(2).random((8, 8, 3)).astype(np.float32)
            out = color.to_rec709(rgb, color.validate(f'lut:{ident}'), limited_range=False)
            np.testing.assert_allclose(out, rgb, atol=1e-5)
            out = color.to_rec709(rgb, color.validate(f'lut:{swap}'), limited_range=False)
            np.testing.assert_allclose(out, rgb[..., ::-1], atol=1e-5)

    def test_malformed_lut_and_unknown_profile_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / 'bad.cube'
            bad.write_text('LUT_3D_SIZE 4\n0 0 0\n')
            with self.assertRaises(ValueError):
                color.validate(f'lut:{bad}')
            with self.assertRaises(FileNotFoundError):
                color.validate(f'lut:{temp}/missing.cube')
        with self.assertRaises(ValueError):
            color.validate('clog9')


class DetectionTests(unittest.TestCase):
    def test_detect_from_metadata_only(self):
        self.assertEqual(color.detect({'color_transfer': 'arib-std-b67'})[0], 'hlg')
        self.assertEqual(color.detect({'color_transfer': 'smpte2084'})[0], 'pq')
        self.assertEqual(color.detect({'color_transfer': 'bt709',
                                       'tags': {'stream.comment': 'Gamma: V-Log'}})[0], 'vlog')
        self.assertEqual(color.detect({'tags': {'format.x': 's-log3'}})[0], 'slog3-cine')
        self.assertEqual(color.detect({'color_transfer': 'bt709', 'tags': {}}), (None, None))

    def test_resolution_order(self):
        self.assertEqual(color.resolve('slog3', 'vlog', 'hlg')[0], 'slog3')
        self.assertEqual(color.resolve('auto', 'vlog', 'hlg')[0], 'vlog')
        self.assertEqual(color.resolve('auto', 'auto', 'hlg')[0], 'hlg')
        self.assertEqual(color.resolve('auto', 'auto', None)[0], 'rec709')


@needs_ffmpeg
class PipelineTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()

    def clip(self, name, *extra, src='color=c=0xC02020:size=320x240:rate=24'):
        path = self.root / 'media' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        core.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', src, '-t', '1',
                  '-c:v', 'libx264', '-pix_fmt', 'yuv420p', *extra, str(path)])
        return path

    def test_untagged_hd_decodes_with_bt709_matrix(self):
        # Encoded with BT.709 coefficients but untagged; FFmpeg would assume BT.601.
        path = self.clip('red.mp4', '-vf', 'scale=1280:720:out_color_matrix=bt709',
                         src='color=c=0xC02020:size=1280x720:rate=24')
        meta = core.probe(path)
        self.assertEqual(core.decode_settings(meta), ('bt709', 'tv'))
        rgb = np.asarray(core.frame(path, 0.5, meta['stream_index'], metadata=meta))
        np.testing.assert_allclose(rgb[360, 640], [0xC0, 0x20, 0x20], atol=4)

    def test_hlg_tagged_clip_detected_converted_and_recorded(self):  # A08
        path = self.clip('hlg.mp4', '-color_trc', 'arib-std-b67', '-color_primaries', 'bt2020',
                         '-colorspace', 'bt2020nc', src='testsrc2=size=320x240:rate=24')
        store = Store(self.root / 'db.sqlite')
        pid = store.create_project('HDR', 'HDR', sample_count=4)
        store.scan_source(store.add_source(pid, path.parent, 'Cam'))
        store.enqueue_analysis(pid)
        WorkerPool(store, self.root / 'cache', min_free_bytes=0,
                   poll_interval=0.02).run_until_idle(timeout=60)
        clip = store.clips(pid)[0]
        self.assertEqual((clip['detected_profile'], clip['profile'], clip['analyzed_profile']),
                         ('hlg', 'hlg', 'hlg'))
        self.assertIn('arib-std-b67', clip['detection_note'])
        self.assertIn('hlg-v1', store.candidates(clip['id'])[0]['preview'])
        k = store.candidates(clip['id'])[0]
        store.save_selection(k['id'], decision='Keep', color_verified=True)
        out = self.root / 'out'
        out.mkdir()
        sidecar = json.loads(store.export_candidate(k['id'], out).with_suffix('.json').read_text())
        self.assertEqual(sidecar['transform']['profile_id'], 'hlg')
        self.assertEqual(sidecar['transform']['decode']['yuv_matrix'], 'bt2020')
        self.assertIn('not a creative grade', sidecar['transform']['note'])

    def test_source_profile_requeues_and_changes_previews(self):
        path = self.clip('cam.mp4', src='testsrc2=size=320x240:rate=24')
        store = Store(self.root / 'db.sqlite')
        pid = store.create_project('Log', 'Log', sample_count=4)
        sid = store.add_source(pid, path.parent, 'Cam A')
        store.scan_source(sid)
        store.enqueue_analysis(pid)
        pool = WorkerPool(store, self.root / 'cache', min_free_bytes=0, poll_interval=0.02)
        pool.run_until_idle(timeout=60)
        clip = store.clips(pid)[0]
        before = store.candidates(clip['id'])
        store.save_selection(before[0]['id'], decision='Keep', color_verified=True)
        self.assertEqual(store.enqueue_analysis(pid), 0)
        store.set_source_profile(sid, 'vlog')
        with self.assertRaises(ValueError):  # previews no longer match the profile
            store.export_candidate(before[0]['id'], self.root)
        self.assertEqual(store.enqueue_analysis(pid), 1)
        pool.run_until_idle(timeout=60)
        after = store.candidates(clip['id'])
        self.assertEqual(store.clips(pid)[0]['analyzed_profile'], 'vlog')
        self.assertEqual([k['id'] for k in after], [k['id'] for k in before])
        self.assertEqual(after[0]['decision'], 'Keep')  # selections survive re-analysis
        self.assertNotEqual(after[0]['preview'], before[0]['preview'])
        a = np.asarray(core.Image.open(before[0]['preview']), dtype=float)
        b = np.asarray(core.Image.open(after[0]['preview']), dtype=float)
        self.assertGreater(np.abs(a - b).mean(), 5)  # the conversion visibly changed pixels


if __name__ == '__main__':
    unittest.main()
