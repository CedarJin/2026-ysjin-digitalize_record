"""Smoke test the frozen arm64 binary using a PATH without developer tools."""
import json
import os
import subprocess
import time
from pathlib import Path

import httpx

root = Path(__file__).resolve().parents[2]
binary = root / 'dist/food-record-backend/food-record-backend'
study = root / 'work/desktop-smoke'
config = {'mode':'study','path':str(study),'capability':'smoke-secret'}
if not study.exists():
    config.update(create=True, name='Desktop smoke study')
process = subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, env={'PATH':'/usr/bin:/bin','HOME':os.environ['HOME'],'TMPDIR':os.environ.get('TMPDIR','/tmp')})
try:
    process.stdin.write(json.dumps(config)+'\n');process.stdin.flush()
    line = process.stdout.readline()
    if not line:
        raise RuntimeError(process.stderr.read())
    ready = json.loads(line)
    url = 'http://127.0.0.1:'+str(ready['port'])
    for attempt in range(100):
        try:
            response=httpx.get(url+'/desktop/state',headers={'X-Desktop-Capability':'smoke-secret'})
            break
        except httpx.ConnectError:
            time.sleep(.05)
    assert response.status_code == 200, response.text
    assert response.json()['name'] == 'Desktop smoke study'
    assert httpx.get(url+'/desktop/state').status_code == 403
    assert httpx.get(url+'/review',headers={'X-Desktop-Capability':'smoke-secret'}).status_code == 200
    print(json.dumps({'startup':True,'no_developer_PATH':True,'unauthorized_denied':True,'port':ready['port']}))
finally:
    process.stdin.close()
    process.wait(timeout=20)
    print('Backend exited:', process.returncode)
