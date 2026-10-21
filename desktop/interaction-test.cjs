'use strict';
const {_electron:electron,expect}=require('playwright/test');
const fs=require('node:fs');const path=require('node:path');const {execFileSync}=require('node:child_process');
const root=path.resolve(__dirname,'..');
(async()=>{
 execFileSync(path.join(root,'.venv/bin/python'),[path.join(root,'scripts/desktop/prepare_qa.py')],{stdio:'ignore'});
 const qa=JSON.parse(fs.readFileSync(path.join(root,'work/desktop-qa-manifest.json')));
 const app=await electron.launch({executablePath:path.join(root,'dist/macos/Food Record-darwin-arm64/Food Record.app/Contents/MacOS/Food Record'),args:['--user-data-dir='+path.join(qa.folder,'interaction-owner')],env:{...process.env,PATH:'/usr/bin:/bin'}});
 try{
  const page=await app.firstWindow();page.setDefaultTimeout(20000);
  await page.getByRole('button',{name:'Open study',exact:true}).waitFor({timeout:60000});
  await app.evaluate(({dialog},study)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[study]});},qa.study);
  await page.getByRole('button',{name:'Open study',exact:true}).click();
  await page.getByRole('link',{name:'Review',exact:true}).click();
  await page.locator('tr').filter({hasText:'P001'}).getByRole('button',{name:'Review now',exact:true}).click();
  await expect(page.locator('#submit')).toHaveText('Submit & next');
  await expect(page.locator('#confirm-sources')).toHaveText('1 page left to check');
  await page.waitForFunction(()=>document.querySelector('#scan').naturalWidth>0);
  await page.locator('#submit').click();
  await expect(page.locator('#submit-error')).toContainText('remaining source pages');
  await page.getByRole('button',{name:'Food items',exact:true}).click();
  await expect(page.getByRole('textbox',{name:'Time normalized',exact:true})).toBeVisible();
  let failedSave=false,failedSubmit=false,submitCalls=0;
  await page.route('**/api/tasks/**',async route=>{
   if(route.request().method()==='PUT'&&!failedSave){failedSave=true;return route.fulfill({status:400,contentType:'application/json',body:JSON.stringify({error:'Synthetic save failure'})});}
   if(route.request().method()==='POST'&&route.request().url().endsWith('/submit')){submitCalls++;if(!failedSubmit){failedSubmit=true;return route.fulfill({status:400,contentType:'application/json',body:JSON.stringify({error:'Synthetic submission failure'})});}}
   return route.continue();
  });
  await page.getByRole('textbox',{name:'Time normalized',exact:true}).fill('08:45');
  await expect(page.locator('#save-error')).toContainText('Synthetic save failure');
  await page.waitForTimeout(6500);await expect(page.locator('#save-error')).toBeVisible();
  await page.getByRole('button',{name:'Try saving again',exact:true}).click();
  await expect(page.locator('#save-error')).toBeHidden();await expect(page.locator('#save-status')).toHaveText('All changes saved');
  await page.locator('#check-current').click();await page.locator('#submit').click();
  await expect(page.locator('#submit-error')).toHaveText('Synthetic submission failure');
  await expect(page.locator('.review-header h1')).toContainText('P001');
  await page.locator('#submit').dblclick();
  await expect(page.locator('.review-header h1')).toContainText('P002');
  expect(submitCalls).toBe(2);
  await expect(page.locator('#review-progress')).toContainText('1 of 2 days completed');
  await page.waitForFunction(()=>document.querySelector('#scan').naturalWidth>0);
  const layout=await page.evaluate(()=>({bottom:document.querySelector('#page-check').getBoundingClientRect().bottom,height:innerHeight}));expect(layout.bottom).toBeLessThanOrEqual(layout.height+1);await page.screenshot({path:path.join(root,'work/interaction-next-review.png')});
  await page.locator('#check-current').click();await page.locator('#submit').click();
  await expect(page.locator('#review-finished')).toContainText('2 of 2 days completed');
  await page.getByRole('button',{name:'View or edit your last review',exact:true}).click();
  await expect(page.getByRole('button',{name:'Edit review',exact:true})).toBeVisible();
  await page.getByRole('link',{name:'Download',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Download results',exact:true})).toBeVisible();
  await expect(page.locator('#approval-summary')).toContainText('2 days still need review or approval');
  await page.getByRole('button',{name:'Download study reviewed data',exact:true}).click();
  await expect(page.locator('#freeze-release')).toBeDisabled();
  await expect(page.locator('#dialog-content')).toContainText('0 approved days');
  await page.locator('#close-dialog').click();
  await page.screenshot({path:path.join(root,'work/interaction-download.png')});
  await page.getByRole('link',{name:'Review',exact:true}).click();
  await page.locator('tr').filter({hasText:'P001'}).getByRole('button',{name:'Preview',exact:true}).click();
  await page.getByRole('button',{name:'Approve for research',exact:true}).click();
  await expect(page.locator('#modal')).toBeVisible();await page.locator('#close-modal').click();
  await page.getByRole('link',{name:'Download',exact:true}).click();
  await expect(page.locator('#approval-summary')).toContainText('1 approved days available');
  await app.evaluate(({session},folder)=>{session.defaultSession.on('will-download',(_event,item)=>item.setSavePath(folder+'/'+item.getFilename()));},qa.folder);
  await page.getByRole('button',{name:'Download study reviewed data',exact:true}).click();
  await page.getByRole('button',{name:'Download approved data',exact:true}).click();
  await expect.poll(()=>fs.existsSync(path.join(qa.folder,'reviewed-data.zip'))).toBeTruthy();
  await page.getByText('Original AI drafts — excludes human corrections',{exact:true}).click();
  await page.getByRole('button',{name:'Download all original drafts',exact:true}).click();
  await expect.poll(()=>fs.existsSync(path.join(qa.folder,'original-drafts.zip'))).toBeTruthy();
  execFileSync(path.join(root,'.venv/bin/python'),['-c',
   `import json,zipfile,sys
from pathlib import Path
p=Path(sys.argv[1])
with zipfile.ZipFile(p/'reviewed-data.zip') as z: reviewed=json.loads(z.read('records.json')); assert 'Data dictionary' in __import__('openpyxl').load_workbook(__import__('io').BytesIO(z.read('records.xlsx'))).sheetnames
assert len(reviewed)==1 and reviewed[0]['subject_id']=='P001'
assert reviewed[0]['content']['events'][0]['time']['normalized']=='08:45'
with zipfile.ZipFile(p/'original-drafts.zip') as z: original=json.loads(z.read('records.json'))
assert len(original)==2 and all(r['status']!='approved' for r in original)
assert all(e['time']['normalized'] is None for r in original for e in r['content']['events'])
`,qa.folder]);
  console.log('Approved download contains human timing edits; original draft download retains unedited baseline');
  console.log('Direct review, page guidance, persistent save error/retry, failed submit retention, submit-once/next, return-to-edit, completion and reviewed download guidance passed');
 }finally{await app.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
