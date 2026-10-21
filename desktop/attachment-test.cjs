'use strict';
const {_electron:electron,expect}=require('playwright/test');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {execFileSync}=require('node:child_process');const root=path.resolve(__dirname,'..');
(async()=>{
 execFileSync(path.join(root,'.venv/bin/python'),[path.join(root,'scripts/desktop/prepare_qa.py')],{stdio:'ignore'});
 const qa=JSON.parse(fs.readFileSync(path.join(root,'work/desktop-qa-manifest.json')));
 const photo=path.join(qa.folder,'clarification.png');
 execFileSync(path.join(root,'.venv/bin/python'),['-c',"from PIL import Image;import sys;Image.new('RGB',(600,800),'white').save(sys.argv[1])",photo]);
 const app=await electron.launch({executablePath:path.join(root,'dist/macos/Food Record-darwin-arm64/Food Record.app/Contents/MacOS/Food Record'),args:['--user-data-dir='+path.join(qa.folder,'attachment-profile')],env:{...process.env,PATH:'/usr/bin:/bin'}});
 let page;
 try{
  page=await app.firstWindow();page.setDefaultTimeout(20000);
  await page.getByRole('button',{name:'Continue a study',exact:true}).waitFor({timeout:60000});
  await app.evaluate(({dialog},study)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[study]});},qa.study);
  await page.getByRole('button',{name:'Continue a study',exact:true}).click();
  await page.locator('[data-add-attachment="R001"]').waitFor();
  const errorState=await page.evaluate(()=>fetch('/desktop/state').then(r=>r.json()));const errorRow=errorState.records.find(r=>r.record_id==='R001');errorRow.state='failed';errorRow.error='api_http_401';
  await page.route('**/desktop/state',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(errorState)}));
  await page.reload();
  const failed=page.locator('tr').filter({has:page.locator('[data-add-attachment="R001"]')});
  await failed.getByText('Details',{exact:true}).click();
  await failed.getByRole('button',{name:'Open settings',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Settings',exact:true})).toBeVisible();
  await page.locator('#close-dialog').click();
  await failed.getByRole('button',{name:'Retry',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Process records',exact:true})).toBeVisible();
  await expect(page.locator('#dialog')).toContainText('1 existing drafts will be reused');
  await page.locator('#close-dialog').click();await page.unroute('**/desktop/state');await page.reload();
  for(const kind of ['text','image']){
   await page.locator('[data-add-attachment="R001"]').click();
   await page.locator('#attachment-form [name="origin"]').selectOption(kind==='text'?'researcher_note':'participant_material');
   if(kind==='text')await page.locator('#attachment-form textarea').fill('Researcher clarification: check cup size against participant note.');
   else {
    const box=await page.locator('#attachment-form [name="file"]').boundingBox();
    const session=await page.context().newCDPSession(page);
    const invalid={items:[],files:[photo,photo],dragOperationsMask:1};
    for(const type of ['dragEnter','dragOver','drop'])await session.send('Input.dispatchDragEvent',{type,x:box.x+20,y:box.y+10,data:invalid});
    await expect(page.locator('#attachment-error')).toHaveText('Drop one PDF, PNG or JPEG attachment.');
    const data={items:[],files:[photo],dragOperationsMask:1};
    for(const type of ['dragEnter','dragOver','drop'])await session.send('Input.dispatchDragEvent',{type,x:box.x+20,y:box.y+10,data});
    await session.detach();
    assert.equal(await page.locator('#attachment-form [name="file"]').evaluate(el=>el.files[0]?.name),'clarification.png');
   }
   await page.getByRole('button',{name:'Check attachment',exact:true}).click();
   await expect(page.locator('[data-source="0"]')).toHaveValue(kind==='text'?'R001-A1':'R001-A2');
   await expect(page.locator('[data-source="0"]')).toBeDisabled();
   await expect(page.getByText(/Existing AI drafts have not incorporated it/)).toBeVisible();
   await page.getByRole('button',{name:'Check import',exact:true}).click();
   await page.locator('#confirm-existing-update').check();
   await page.getByRole('button',{name:'Import records',exact:true}).click();
  }
  await page.locator('[data-manual="R001"]').click();
  await page.waitForFunction(()=>document.querySelector('#scan')?.naturalWidth>0);
  await expect(page.locator('#source-page option')).toHaveCount(3);
  await expect(page.locator('details.item')).toHaveCount(1);
  await page.locator('#source-page').selectOption('1');
  await expect(page.locator('#metadata-banner')).toContainText('Researcher note');
  await page.locator('#source-page').selectOption('2');
  await expect(page.locator('#metadata-banner')).toContainText('Participant material');
  await page.getByRole('button',{name:'Issues',exact:true}).click();
  await expect(page.locator('#editor')).toContainText('has not been interpreted by AI');
  await page.getByRole('button',{name:'Food items',exact:true}).click();
  await page.getByRole('textbox',{name:'Time normalized',exact:true}).focus();
  await page.keyboard.press('Meta+Enter');
  await page.getByRole('textbox',{name:'Response',exact:true}).fill('Additional food row near the bottom');
  await page.locator('dialog[open] [data-accept]').click();
  await expect(page.locator('details.item')).toHaveCount(2);
  await expect(page.locator('#food-item-count')).toHaveText('2');
  await expect(page.locator('details.item').last().locator('[data-value]').first()).toBeFocused();
  await page.screenshot({path:path.join(root,'work/interaction-attachment-review.png')});
  const evidence=execFileSync(path.join(root,'.venv/bin/python'),['-c',`import sys\nfrom food_record_digitizer.workspace import Workspace\nfrom pathlib import Path\nfrom food_record_digitizer.db import ExtractionRun,Record\nfrom sqlalchemy import select\nw=Workspace(Path(sys.argv[1]))\nwith w.factory() as s:\n assert len(list(s.scalars(select(Record))))==2\n assert not list(s.scalars(select(ExtractionRun)))\nprint('No extra intake records or AI requests')`,qa.study],{encoding:'utf8'});console.log(evidence.trim());
  console.log('Packaged text/image attachment addition, shared numbering, locked associations, retained food draft and source provenance passed');
 }catch(error){if(page){await page.screenshot({path:path.join(root,'work/attachment-gui-failure.png')}).catch(()=>{});console.log((await page.locator('body').innerText()).slice(0,4500));}throw error;}
 finally{await app.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
