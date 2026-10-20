"""Build a small reviewer distribution from an already-built wheel."""

import stat
import zipfile
from pathlib import Path

from food_record_digitizer import __version__

root = Path(__file__).resolve().parents[1]
wheel = root / "dist" / f"food_record_digitizer-{__version__}-py3-none-any.whl"
if not wheel.is_file():
    raise SystemExit("Build the package wheel first.")
bootstrap = r"""from pathlib import Path
import subprocess
import sys
import venv

root = Path(__file__).resolve().parent
if sys.version_info < (3, 11):
    raise SystemExit('Python 3.11 or newer is required. Install Python, then reopen this launcher.')
env = root / '.reviewer-env'
python = env / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
marker = env / 'ready'
try:
    if not marker.exists():
        print('First-time setup: installing the reviewer. Internet access is needed for Python dependencies only.')
        venv.EnvBuilder(with_pip=True).create(env)
        wheel = next(root.glob('*.whl'))
        subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(root/'requirements-tested.txt'), str(wheel)], check=True)
        marker.write_text(wheel.name)
    subprocess.run([str(python), '-c', 'from food_record_digitizer.offline_web import main; main()'], check=True)
except Exception as exc:
    print('Unable to start the reviewer:', type(exc).__name__)
    input('Press Enter to close. Share this setup error with your coordinator.')
    raise SystemExit(1)
"""
mac = """#!/bin/sh
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.11 or newer is required. Ask your coordinator to install it."
  read answer
  exit 1
fi
python3 start_reviewer.py
"""
windows = """@echo off
cd /d "%~dp0"
py -3 start_reviewer.py
if errorlevel 1 pause
"""
readme = """OFFLINE REVIEWER

1. Extract this folder to a writable location. Keep its files together.
2. Python 3.11+ is required. Ask your coordinator to arrange installation if needed.
3. On macOS, open Open Reviewer.command. On Windows, open Open Reviewer.bat.
   Linux users can run python3 start_reviewer.py.
4. First launch installs pinned Python dependencies and needs internet access.
   Later review sessions work offline. No account, API key or Poppler is needed
   for packages exported by version 0.4.0 or newer.
5. In the browser, choose review-package-<ID>.zip from your coordinator.
   Do not select offline-reviewer-<version>.zip: it contains this application,
   not review tasks. Your coordinator exports tasks from the project page.
6. Open each day, check the scans and edit the food entries. Save before closing.
   Submit each completed day, then Export results and send that ZIP back.
7. Reopen the SAME package to resume. Your original package is never modified.

Work is saved under .food-record-reviewer in your user home folder.
Keep the launcher window running while reviewing. Close it when finished.
To open a different package, start the launcher again in a separate window.
Do not delete the saved-work folder until your coordinator confirms receipt.

This is a Python launcher bundle, not a standalone signed desktop installer.
macOS runtime acceptance is recorded by the project; Windows/Linux launchers
are provided but need local installation verification before distribution.
"""
entries = {
    wheel.name: wheel.read_bytes(),
    "start_reviewer.py": bootstrap.encode(),
    "Open Reviewer.command": mac.encode(),
    "Open Reviewer.bat": windows.encode(),
    "START HERE.txt": readme.encode(),
    "requirements-tested.txt": (root / "requirements-tested.txt").read_bytes(),
}
out = root / "dist" / f"offline-reviewer-{__version__}.zip"
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for name, data in entries.items():
        info = zipfile.ZipInfo("Offline Reviewer/" + name, (2026, 10, 19, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = (stat.S_IFREG | (0o755 if name.endswith(".command") else 0o644)) << 16
        z.writestr(info, data)
print(out)
