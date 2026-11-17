'use strict';
const {_electron:electron,expect}=require('playwright/test'),fs=require('node:fs'),path=require('node:path'),{execFileSync}=require('node:child_process');
const root=path.resolve(__dirname,'..');
execFileSync(path.join(root,'.venv/bin/python'),[path.join(root,'scripts/desktop/prepare_qa.py')],{stdio:'ignore'});
const qa=JSON.parse(fs.readFileSync(path.join(root,'work/desktop-qa-manifest.json')));
execFileSync(path.join(root,'.venv/bin/python'),['-c',`import sys\nfrom pathlib import Path\nsys.path.insert(0,'tests')\nfrom test_day_review import add_source\nfrom food_record_digitizer.workspace import Workspace\nw=Workspace(Path(sys.argv[1]))\nfor i in range(3,25): add_source(w,Path(sys.argv[2]),rid=f'R{i:03}',sid=f'P{i:03}')`,qa.study,qa.folder],{cwd:root});
(async()=>{
 const app=await electron.launch({executablePath:path.join(root,'dist/macos/Food Record-darwin-arm64/Food Record.app/Contents/MacOS/Food Record'),args:['--user-data-dir='+path.join(qa.folder,'send-selection')],env:{...process.env,PATH:'/usr/bin:/bin'}});
 try{
 const page=await app.firstWindow();page.setDefaultTimeout(20000);
 await page.getByRole('button',{name:'Continue a study',exact:true}).waitFor({timeout:60000});
 await app.evaluate(({dialog},file)=>dialog.showOpenDialog=async()=>({canceled:false,filePaths:[file]}),qa.study);
 await page.getByRole('button',{name:'Continue a study',exact:true}).click();await page.locator('#select-all').check();await page.locator('#review-selected').click();await page.locator('#back').click();
 async function send(name,split){
  await page.locator('#batch-offline').click();await expect(page.locator('#send-selection')).toContainText('24 records · 24 review days selected');await expect(page.locator('#offline-size')).toBeHidden();
  await page.getByText('New reviewer? Add name and email',{exact:true}).click();await page.locator('#offline-add-reviewer [name=name]').fill(name);await page.locator('#offline-add-reviewer [name=email]').fill(name+'@example.test');await page.locator('#offline-add-reviewer button').click();await expect(page.locator('#offline-add-status')).toContainText('Reviewer added');
  if(split){await page.getByText('Split into smaller ZIP files (optional)',{exact:true}).click();await page.locator('#offline-split').check();await page.locator('#offline-size').fill('10');}
  const response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/offline/export'&&r.request().method()==='POST'&&!r.request().postDataJSON().preview);await page.locator('#offline-create').click();const result=await response;expect(result.ok()).toBeTruthy();const packages=await result.json();expect(packages.map(p=>p.days)).toEqual(split?[10,10,4]:[24]);await expect(page.getByRole('heading',{name:'Ready to send',exact:true})).toBeVisible();await page.locator('#close-modal').click();
 }
 await send('OneZip',false);await send('SplitZip',true);
 // Allocate the same synthetic scope to distinct reviewers and verify actual persisted packages.
 await page.locator('#batch-offline').click();
 async function addPerson(name){
  const form=page.locator('#offline-add-reviewer');
  if(!await form.isVisible())await page.getByText('New reviewer? Add name and email',{exact:true}).click();
  await form.locator('[name=name]').fill(name);await form.locator('[name=email]').fill(name+'@example.test');await form.locator('button').click();
  await expect(page.locator('[data-reviewer]').last()).not.toHaveValue('');
 }
 await addPerson('TeamA');await page.locator('#send-add-person').click();await addPerson('TeamB');await page.locator('#send-add-person').click();await addPerson('TeamC');
 await expect(page.locator('#send-reviewers')).toContainText('8 review days');
 await page.locator('#send-mode').selectOption('custom');
 const count=page.locator('[data-reviewer-count]');await count.nth(0).fill('10');await count.nth(1).fill('10');await count.nth(2).fill('3');
 await page.locator('#offline-create').click();await expect(page.locator('#offline-preview-result')).toContainText('Allocate all selected days');
 await count.nth(2).fill('4');
 const first=await page.locator('[data-reviewer]').nth(0).inputValue(),third=await page.locator('[data-reviewer]').nth(2).inputValue();
 await page.locator('[data-reviewer]').nth(2).selectOption(first);await page.locator('#offline-create').click();await expect(page.locator('#offline-preview-result')).toContainText('only appear once');await page.locator('[data-reviewer]').nth(2).selectOption(third);
 const issued=[];page.on('response',async r=>{if(new URL(r.url()).pathname==='/api/offline/export'&&r.request().method()==='POST'&&!r.request().postDataJSON().preview&&r.ok())issued.push({request:r.request().postDataJSON(),packages:await r.json()});});
 await page.locator('#offline-create').click();await expect(page.getByRole('heading',{name:'Ready to send',exact:true})).toBeVisible({timeout:60000});
 expect(issued.map(x=>x.packages.reduce((n,p)=>n+p.days,0))).toEqual([10,10,4]);expect(new Set(issued.flatMap(x=>x.request.unit_ids)).size).toBe(24);
 for(const name of ['TeamA','TeamB','TeamC'])await expect(page.getByRole('link',{name:new RegExp('Download '+name)})).toBeVisible();
 const saved=await page.evaluate(()=>fetch('/api/offline/packages').then(r=>r.json()));
 for(const [i,name] of ['TeamA','TeamB','TeamC'].entries()){const row=saved.find(p=>p.reviewer===name);expect(row.days).toBe([10,10,4][i]);expect(row.email).toBe(name.toLowerCase()+'@example.test');}
 // A preflight failure must create no packages; a later export failure keeps finished downloads.
 await page.locator('#close-modal').click();await page.locator('#batch-offline').click();await addPerson('PartialA');await page.locator('#send-add-person').click();await addPerson('PartialB');
 let committed=0;
 await page.route('**/api/offline/export',async route=>{const body=route.request().postDataJSON();if(body.preview)return route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({error:'Synthetic preflight failure'})});committed++;return route.continue();});
 await page.locator('#offline-create').click();await expect(page.locator('#offline-preview-result')).toContainText('Synthetic preflight failure');expect(committed).toBe(0);await page.unroute('**/api/offline/export');
 await page.route('**/api/offline/export',async route=>{const body=route.request().postDataJSON();if(!body.preview&&++committed===2)return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'Synthetic packaging failure'})});return route.continue();});
 await page.locator('#offline-create').click();await expect(page.getByRole('heading',{name:'Some packages are ready',exact:true})).toBeVisible({timeout:60000});await expect(page.getByRole('link',{name:/Download PartialA/})).toBeVisible();await expect(page.locator('#modal-body')).toContainText('1 of 2 reviewers completed');await page.unroute('**/api/offline/export');
 console.log('Multi-reviewer allocation: valid counts, duplicate rejection, 24 distinct days, correct recipient identities and downloads passed.');
 console.log('24 selected records: one ZIP without quantity input; optional split produces 10+10+4 with no records omitted.');
 }finally{await app.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
