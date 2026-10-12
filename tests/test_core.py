import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from core import analyze, export_still, inventory, run, sample_times, safe_label


class CoreTests(unittest.TestCase):
    def test_sampling_bounds(self):
        for duration in (.01, 1, 3600):
            times = sample_times(duration, 12)
            self.assertEqual(len(times), 12)
            self.assertTrue(all(0 < t < duration for t in times))
        for duration in (0, -1, float('nan')):
            with self.assertRaises(ValueError):
                sample_times(duration, 12)

    def test_label(self):
        self.assertNotIn('/', safe_label('../../escape'))
        self.assertEqual(safe_label(''), 'Other')

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
    def test_actual_decode_export_and_source_integrity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source with spaces'
            source.mkdir()
            video = source / 'test clip.mp4'
            run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                 'testsrc2=size=320x240:rate=24', '-t', '1', '-c:v', 'mpeg4', str(video)])
            original = hashlib.sha256(video.read_bytes()).hexdigest()
            clip = inventory(source)[0]
            analysis = analyze(clip, root / 'cache', 4)
            self.assertEqual(len(analysis['frames']), 4)
            self.assertEqual(analysis, analyze(clip, root / 'cache', 4))
            candidate = analysis['frames'][0]
            review = dict(decision='Keep', color_verified=True, rank='A', tag='Action', period='Day')
            out = export_still(analysis, candidate, review, root, 'TestProject')
            self.assertTrue(out.exists())
            self.assertTrue(out.with_suffix('.json').exists())
            self.assertTrue((out.parent / 'stills_index.csv').exists())
            with self.assertRaises(FileExistsError):
                export_still(analysis, candidate, review, root, 'TestProject')
            with self.assertRaises(ValueError):
                export_still(analysis, candidate, {'decision': 'Reject'}, root)
            with self.assertRaises(ValueError):
                export_still(analysis, candidate, {'decision': 'Keep'}, root)
            with self.assertRaises(FileNotFoundError):
                export_still(analysis, candidate, review, root / 'unmounted')
            self.assertEqual(original, hashlib.sha256(video.read_bytes()).hexdigest())
            video.write_bytes(b'changed')
            with self.assertRaises(ValueError):
                analyze(clip, root / 'cache', 4)


if __name__ == '__main__':
    unittest.main()
