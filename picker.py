"""Native folder chooser for the local app.

A browser cannot reveal real file-system paths to a web page, but the Streamlit
server runs on the same computer as the footage, so it can open the operating
system's own folder dialog. Only works when the app runs locally (the default
127.0.0.1 binding); the dialog appears on the computer running the server.
"""
import shutil
import subprocess
import sys
from pathlib import Path

# The prompt and start folder are passed as osascript arguments, never spliced into
# the script text, so no quoting or injection issues.
MAC_SCRIPT = ['on run argv',
              'set chosen to choose folder with prompt (item 1 of argv)',
              'return POSIX path of chosen',
              'end run']
MAC_SCRIPT_WITH_START = ['on run argv',
                         'set chosen to choose folder with prompt (item 1 of argv) '
                         'default location (POSIX file (item 2 of argv))',
                         'return POSIX path of chosen',
                         'end run']


def available():
    return sys.platform == 'darwin' or bool(shutil.which('zenity') or shutil.which('kdialog'))


def _start_folder(start):
    if not start:
        return None
    path = Path(start).expanduser()
    return str(path) if path.is_dir() else None


def _clean(output):
    path = output.strip()
    return (path.rstrip('/') or '/') if path else None


def pick_folder(prompt='Choose a folder', start=None, timeout=600):
    """Show a native folder dialog. Returns the chosen path, or None if cancelled."""
    start = _start_folder(start)
    if sys.platform == 'darwin':
        lines = MAC_SCRIPT_WITH_START if start else MAC_SCRIPT
        args = ['osascript']
        for line in lines:
            args += ['-e', line]
        args += [prompt] + ([start] if start else [])
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if p.returncode:
            if '-128' in p.stderr:  # "User canceled."
                return None
            raise RuntimeError(p.stderr.strip() or 'Folder dialog failed.')
        return _clean(p.stdout)
    if shutil.which('zenity'):
        args = ['zenity', '--file-selection', '--directory', '--title', prompt]
        if start:
            args += ['--filename', start.rstrip('/') + '/']
    elif shutil.which('kdialog'):
        args = ['kdialog', '--getexistingdirectory', start or str(Path.home()), '--title', prompt]
    else:
        raise RuntimeError('No folder dialog is available on this system; type the path instead.')
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if p.returncode == 1:  # cancelled
        return None
    if p.returncode:
        raise RuntimeError(p.stderr.strip() or 'Folder dialog failed.')
    return _clean(p.stdout)


MAC_FILE_SCRIPT = ['on run argv',
                   'set chosen to choose file with prompt (item 1 of argv)',
                   'return POSIX path of chosen',
                   'end run']


def pick_file(prompt='Choose a file', timeout=600):
    """Show a native file dialog. Returns the chosen path, or None if cancelled."""
    if sys.platform == 'darwin':
        args = ['osascript']
        for line in MAC_FILE_SCRIPT:
            args += ['-e', line]
        p = subprocess.run(args + [prompt], capture_output=True, text=True, timeout=timeout)
        if p.returncode:
            if '-128' in p.stderr:
                return None
            raise RuntimeError(p.stderr.strip() or 'File dialog failed.')
        return p.stdout.strip() or None
    if shutil.which('zenity'):
        args = ['zenity', '--file-selection', '--title', prompt]
    elif shutil.which('kdialog'):
        args = ['kdialog', '--getopenfilename', str(Path.home()), '--title', prompt]
    else:
        raise RuntimeError('No file dialog is available on this system; type the path instead.')
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if p.returncode == 1:
        return None
    if p.returncode:
        raise RuntimeError(p.stderr.strip() or 'File dialog failed.')
    return p.stdout.strip() or None
