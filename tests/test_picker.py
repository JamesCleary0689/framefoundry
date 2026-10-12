"""Native folder picker: argument construction and cancel handling (no real dialog)."""
import subprocess
import tempfile
import unittest
from unittest import mock

import picker


def done(code=0, out='', err=''):
    return subprocess.CompletedProcess([], code, out, err)


class MacPickerTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(picker.sys, 'platform', 'darwin')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_returns_chosen_path_and_passes_text_as_arguments(self):
        with tempfile.TemporaryDirectory() as start, \
                mock.patch.object(picker.subprocess, 'run',
                                  return_value=done(out='/Volumes/Card A/Clips/\n')) as run:
            self.assertEqual(picker.pick_folder('Pick "it"', start), '/Volumes/Card A/Clips')
        args = run.call_args.args[0]
        self.assertEqual(args[0], 'osascript')
        self.assertEqual(args[-2:], ['Pick "it"', start])  # data, not script text
        self.assertNotIn('Pick', ' '.join(a for a in args[:-2]))

    def test_missing_start_folder_is_ignored(self):
        with mock.patch.object(picker.subprocess, 'run', return_value=done(out='/x/\n')) as run:
            picker.pick_folder('Pick', '/definitely/not/here')
        self.assertEqual(run.call_args.args[0][-1], 'Pick')

    def test_cancel_returns_none_and_errors_raise(self):
        with mock.patch.object(picker.subprocess, 'run',
                               return_value=done(1, err='execution error: User canceled. (-128)')):
            self.assertIsNone(picker.pick_folder())
        with mock.patch.object(picker.subprocess, 'run', return_value=done(1, err='boom')):
            with self.assertRaises(RuntimeError):
                picker.pick_folder()


class LinuxPickerTests(unittest.TestCase):
    def test_zenity_and_no_dialog(self):
        with mock.patch.object(picker.sys, 'platform', 'linux'), \
                mock.patch.object(picker.shutil, 'which',
                                  side_effect=lambda n: '/usr/bin/zenity' if n == 'zenity' else None):
            with mock.patch.object(picker.subprocess, 'run', return_value=done(out='/media/x\n')):
                self.assertEqual(picker.pick_folder(), '/media/x')
            with mock.patch.object(picker.subprocess, 'run', return_value=done(1)):
                self.assertIsNone(picker.pick_folder())
        with mock.patch.object(picker.sys, 'platform', 'linux'), \
                mock.patch.object(picker.shutil, 'which', return_value=None):
            self.assertFalse(picker.available())
            with self.assertRaises(RuntimeError):
                picker.pick_folder()


if __name__ == '__main__':
    unittest.main()
