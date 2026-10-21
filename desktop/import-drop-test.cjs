'use strict';
const {_electron:electron,expect}=require('playwright/test');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {execFileSync}=require('node:child_process');
const root=path.resolve(__dirname,'..');
async function drop(page,files){
 const session=await page.context().newCDPSession(page);
 const data={items:[],files,dragOperationsMask:1};
 try{for(const type of ['dragEnter','dragOver','drop'])await session.send('Input.dispatchDragEvent',{type,x:300,y:200,data});}
 finally{await session.detach().catch(()=>{});}
}
(async()=>{
 execFileSync(path.join(root,'.venv/bin/python'),[path.join(root,'scripts/desktop/prepare_qa.py')],{stdio:'ignore'});
 const qa=JSON.parse(fs.readFileSync(path.join(root,'work/desktop-qa-manifest.json')));
 const incoming=path.join(qa.folder,'Incoming records'),study=path.join(qa.folder,'New researcher study');
 fs.mkdirSync(path.join(incoming,'sub'),{recursive:true});
 execFileSync(path.join(root,'.venv/bin/python'),['-c',`import sys\nfrom pathlib import Path\nsys.path.insert(0,'tests')\nfrom test_ingest import pdf\np=Path(sys.argv[1]); pdf(p/'Unsorted A.pdf');pdf(p/'sub'/'Unsorted B.pdf',2);pdf(p.parent/'Another scan.pdf',3)`,incoming],{cwd:root});
 fs.writeFileSync(path.join(incoming,'participants.csv'),'participant,date\nP100,\nP200,2026-10-21\n');
 fs.writeFileSync(path.join(incoming,'unrelated.md'),'not a source');fs.writeFileSync(path.join(incoming,'.env'),'not imported');
 const corrupt=path.join(qa.folder,'wrong.zip');fs.writeFileSync(corrupt,'not a zip');
 const returned=path.join(qa.folder,'returned.zip');
 execFileSync(path.join(root,'.venv/bin/python'),['-c',`import sys\nfrom pathlib import Path\nsys.path.insert(0,'tests')\nfrom test_offline import result_for\nfrom food_record_digitizer.offline_archive import pack\nPath(sys.argv[2]).write_bytes(pack(result_for({'path':sys.argv[1]}),[]))`,qa.packages[0].path,returned],{cwd:root});
 const profile=path.join(qa.folder,'drop-profile');
 const app=await electron.launch({executablePath:path.join(root,'dist/macos/Food Record-darwin-arm64/Food Record.app/Contents/MacOS/Food Record'),args:['--user-data-dir='+profile],env:{...process.env,PATH:'/usr/bin:/bin'}});
 let page;
 try{
  page=await app.firstWindow();page.setDefaultTimeout(20000);
  await page.getByRole('button',{name:'Start a new study',exact:true}).waitFor({timeout:60000});
  await expect(page.locator('#study-name')).toHaveCount(0);
  await app.evaluate(({dialog},filePath)=>{dialog.showSaveDialog=async()=>({canceled:false,filePath});},study);
  await page.getByRole('button',{name:'Start a new study',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Add records',exact:true})).toBeVisible();
  await page.getByText('Study name and location',{exact:true}).click();
  await page.locator('#study-name-edit').fill('Research study renamed');
  await drop(page,[incoming]);
  await expect(page.getByRole('heading',{name:'Check files and metadata',exact:true})).toBeVisible();
  await expect(page.locator('[data-source="0"]')).toHaveValue('R0001');await expect(page.locator('[data-source="1"]')).toHaveValue('R0002');
  await expect(page.locator('[data-meta="0"][data-field="record_id"]')).toHaveValue('');
  await page.locator('[data-meta="0"][data-field="record_id"]').selectOption('R0002');
  await page.locator('[data-meta="1"][data-field="record_id"]').selectOption('R0001');
  await page.getByRole('button',{name:'Switch study',exact:true}).click();
  await page.getByRole('button',{name:'Research study renamed',exact:true}).click();
  await page.getByRole('button',{name:'Resume unfinished import',exact:true}).click();
  await expect(page.locator('[data-meta="0"][data-field="record_id"]')).toHaveValue('R0002');
  await page.getByRole('button',{name:'Check import',exact:true}).click();await page.getByRole('button',{name:'Import records',exact:true}).click();
  await expect(page.locator('[data-edit-metadata="R0001"]')).toBeVisible();
  let state=await page.evaluate(()=>fetch('/desktop/state').then(r=>r.json()));
  assert.equal(state.name,'Research study renamed');assert.equal(state.records.length,2);
  assert.equal(state.records.find(r=>r.record_id==='R0002').metadata.subject_id,'P100');assert.equal(state.records.find(r=>r.record_id==='R0002').metadata.date,null);
  assert(fs.existsSync(path.join(study,'sources/records/R0001/R0001.pdf')));assert(fs.existsSync(path.join(incoming,'Unsorted A.pdf')));
  await page.locator('[data-edit-metadata="R0001"]').click();await page.locator('#metadata-participant').fill('P900');await page.locator('#metadata-date').fill('');
  await page.getByRole('button',{name:'Check changes',exact:true}).click();await page.getByRole('button',{name:'Save metadata',exact:true}).click();
  await expect(page.locator('#dialog')).not.toBeVisible();
  state=await page.evaluate(()=>fetch('/desktop/state').then(r=>r.json()));assert.equal(state.records.find(r=>r.record_id==='R0001').metadata.subject_id,'P900');
  await drop(page,[path.join(qa.folder,'Another scan.pdf')]);
  await expect(page.locator('[data-source="0"]')).toHaveValue('R0003');
  await page.locator('[data-meta="0"][data-field="subject_id"]').fill('P300');
  await page.getByRole('button',{name:'Check import',exact:true}).click();await page.getByRole('button',{name:'Import records',exact:true}).click();
  await page.getByRole('button',{name:'Download',exact:true}).click();
  const backup=path.join(qa.folder,'study-backup.zip');
  await app.evaluate(({session},target)=>{session.defaultSession.on('will-download',(_event,item)=>item.setSavePath(target));},backup);
  await page.getByText('Study backup',{exact:true}).click();await page.getByRole('button',{name:'Save backup',exact:true}).click();
  await expect.poll(()=>fs.existsSync(backup)&&fs.statSync(backup).size>0).toBeTruthy();
  console.log('Path-first create, recursive folder drop, explicit two-column mapping, saved import resume, sequential IDs, metadata edit and backup passed');
  await drop(page,[qa.packages[0].path]);
  await expect(page.getByRole('heading',{name:'Review package',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Open',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#scan')?.naturalWidth>0);
  const time=()=>page.getByRole('textbox',{name:'Time normalized',exact:true});
  await time().fill('09:15');const before=page.url();await drop(page,[corrupt]);
  await expect(page.locator('#desktop-drop-status')).toContainText('damaged');assert.equal(page.url(),before);await expect(time()).toHaveValue('09:15');
  await time().fill('10:10');await drop(page,[qa.packages[0].path]);
  await expect(page.getByRole('heading',{name:'Review package',exact:true})).toBeVisible();await page.getByRole('button',{name:'Open',exact:true}).click();await expect(time()).toHaveValue('10:10');
  await page.route('**/api/tasks/**',route=>route.request().method()==='PUT'?route.fulfill({status:400,contentType:'application/json',body:JSON.stringify({error:'Synthetic save failure during drop'})}):route.continue());
  await time().fill('11:11');const active=page.url();await drop(page,[qa.packages[0].path]);
  await expect(page.locator('#save-error')).toContainText('Synthetic save failure during drop');assert.equal(page.url(),active);await expect(time()).toHaveValue('11:11');
  await page.unroute('**/api/tasks/**');await page.getByRole('button',{name:'Try saving again',exact:true}).click();await expect(page.locator('#save-status')).toHaveText('All changes saved');
  await drop(page,[returned]);await expect(page.locator('#desktop-drop-status')).toContainText('returned results');await expect(time()).toHaveValue('11:11');
  const restored=path.join(qa.folder,'Restored via drop');await app.evaluate(({dialog},filePath)=>{dialog.showSaveDialog=async()=>({canceled:false,filePath});},restored);
  await drop(page,[backup]);
  await expect(page.getByRole('heading',{name:'Research study renamed',exact:true})).toBeVisible({timeout:60000});
  state=await page.evaluate(()=>fetch('/desktop/state').then(r=>r.json()));assert.equal(state.records.length,3);assert.equal(state.queue.active,0);assert.equal(state.queue.queued,0);assert.equal(state.has_key,false);
  await page.screenshot({path:path.join(root,'work/interaction-import-drop.png')});
  console.log('Native review ZIP drop, invalid ZIP protection, same-package resume, failed-save switch protection, result guidance and dropped-backup restore passed; no AI requests');
 }catch(error){if(page){await page.screenshot({path:path.join(root,'work/interaction-drop-failure.png')}).catch(()=>{});console.log((await page.locator('body').innerText()).slice(0,5000));}throw error;}
 finally{await app.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
