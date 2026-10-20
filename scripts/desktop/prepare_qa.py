"""Synthetic test studies; never reads credentials or calls AI."""
import json
import sys
from pathlib import Path
from uuid import uuid4

root=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root/'tests'))
from test_day_review import add_source
from test_ingest import pdf
from food_record_digitizer.workspace import Workspace
from food_record_digitizer.desktop_server import identity
from food_record_digitizer.day_review import DayReviewService
from food_record_digitizer.review import DEFAULT_POLICY
from food_record_digitizer.offline import OfflineManager

folder=root/'work'/('desktop-qa-'+uuid4().hex[:8])
folder.mkdir()
workspace=Workspace.initialize(folder/'Study with spaces 研究','desktop-qa','Desktop workflow test')
add_source(workspace,folder,rid='R001',sid='P001')
add_source(workspace,folder,rid='R002',sid='P002')
owner=identity(workspace)['id']
service=DayReviewService(workspace)
service.set_policy(owner,{**DEFAULT_POLICY,'sample_percent':0,'high_risk_flags':False})
rows=service.queue(owner)
manager=OfflineManager(workspace)
alice=manager.reviewer(owner,'Alice Example','alice@example.test')
bob=manager.reviewer(owner,'Bob Example','bob@example.test')
unit=next(r['record_id'] for r in rows if r['source_record_ids']==['R002'])
packages=[]
for reviewer in [alice,bob]:
 packages.extend(manager.export(owner,reviewer,[unit],units_per_package=1))
pdf(folder/'New scan.pdf');pdf(folder/'Attachment.pdf')
(folder/'metadata.csv').write_text('record_id,subject_id,date\nR003,P003,\nR999,P999,2026-01-03\n')
manifest={'study':str(workspace.path),'folder':str(folder),'packages':packages}
(root/'work/desktop-qa-manifest.json').write_text(json.dumps(manifest,indent=2))
print(json.dumps(manifest,indent=2))
