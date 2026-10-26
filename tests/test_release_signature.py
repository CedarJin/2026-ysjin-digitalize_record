"""Exercise real macOS resource sealing, including rejection after tampering."""

import importlib.util
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS codesign integration")
def test_archive_signature_gate_rejects_modified_resource(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/desktop/release_archive.py"
    sys.path.insert(0, str(script.parent))
    try:
        spec = importlib.util.spec_from_file_location("release_archive", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    app = tmp_path / "Test.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Resources").mkdir()
    shutil.copyfile("/bin/echo", app / "Contents/MacOS/Test")
    (app / "Contents/MacOS/Test").chmod(0o755)
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleExecutable": "Test",
                "CFBundleIdentifier": "org.foodrecord.signature-test",
                "CFBundlePackageType": "APPL",
            }
        )
    )
    resource = app / "Contents/Resources/test.txt"
    resource.write_text("Original resource")
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", str(app)], check=True)
    module.verify_signature(app)
    resource.write_text("Modified after signing")
    with pytest.raises(subprocess.CalledProcessError):
        module.verify_signature(app)
