"""Verify an actual result exported by the packaged reviewer UI."""
import json
from pathlib import Path

from food_record_digitizer.desktop_server import identity
from food_record_digitizer.offline import OfflineManager
from food_record_digitizer.offline_archive import unpack
from food_record_digitizer.workspace import Workspace

root=Path(__file__).resolve().parents[2]
qa=json.loads((root/'work/desktop-qa-manifest.json').read_text())
workspace=Workspace(Path(qa['study']))
manager=OfflineManager(workspace)
owner=identity(workspace)['id']
result=(Path(qa['folder'])/'alice-results.zip').read_bytes()
first=manager.import_result(owner,result)
assert first['tasks'][0]['status']=='accepted',first
assert manager.import_result(owner,result)['status']=='duplicate'
bob,_=unpack(Path(qa['packages'][1]['path']).read_bytes(),'task')
assert not any(p['reviewed'] for unit in bob['units'] for p in unit['content']['pages'])
print('Actual desktop return accepted; duplicate protected; Bob retains independent unreviewed baseline.')
