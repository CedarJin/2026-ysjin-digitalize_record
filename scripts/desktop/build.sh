#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
test "$(uname -m)" = arm64 || { echo 'Build on an Apple Silicon Mac.'; exit 1; }
export PYINSTALLER_CONFIG_DIR="$PWD/build/pyinstaller-cache"
.venv/bin/python -m PyInstaller --noconfirm --clean --onedir --name food-record-backend --target-architecture arm64 --distpath dist --workpath build/desktop --specpath build --collect-all pypdfium2 --collect-all pypdfium2_raw --collect-all food_record_digitizer --copy-metadata food-record-digitizer --copy-metadata pillow --copy-metadata pydantic --copy-metadata httpx --hidden-import uvicorn.logging --hidden-import uvicorn.loops.auto --hidden-import uvicorn.protocols.http.auto --hidden-import uvicorn.protocols.websockets.auto --hidden-import uvicorn.lifespan.on scripts/desktop/backend_entry.py
cd desktop
npm ci
npm run package
