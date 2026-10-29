'use strict';
const {_electron:electron,expect}=require('playwright/test');
const fs=require('node:fs'),path=require('node:path'),{execFileSync}=require('node:child_process');
const root=path.resolve(__dirname,'..');
execFileSync(path.join(root,'.venv/bin/python'),[path.join(root,'scripts/desktop/prepare_qa.py')],{stdio:'ignore'});
const qa=JSON.parse(fs.readFileSync(path.join(root,'work/desktop-qa-manifest.json')));
execFileSync(path.join(root,'.venv/bin/python'),['-c',`import copy,sys\nfrom sqlalchemy import select\nfrom food_record_digitizer.workspace import Workspace\nfrom food_record_digitizer import db\nw=Workspace(sys.argv[1])\nwith w.factory.begin() as s:\n r=s.scalar(select(db.Record).where(db.Record.record_id=='R001'))\n c=s.scalar(select(db.Candidate).where(db.Candidate.record_pk==r.id))\n data=copy.deepcopy(c.content)\n item=copy.deepcopy(data['items'][0]);item['item_id']='second';data['items'].append(item);c.content=data\n`,qa.study]);
async function launch(profile){const app=await electron.launch({executablePath:path.join(root,'dist/macos/Food Record-darwin-arm64/Food Record.app/Contents/MacOS/Food Record'),args:['--user-data-dir='+path.join(qa.folder,profile)],env:{...process.env,PATH:'/usr/bin:/bin'}});const page=await app.firstWindow();page.setDefaultTimeout(20000);await page.getByRole('button',{name:'Continue a study',exact:true}).waitFor({timeout:60000});return {app,page};}
async function choose(app,file){await app.evaluate(({dialog},file)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[file]});},file);}
(async()=>{
 let {app,page}=await launch('date-owner');
 try{
  await choose(app,qa.study);await page.getByRole('button',{name:'Continue a study',exact:true}).click();
  await page.getByRole('link',{name:'Review',exact:true}).click();
  await page.locator('tr').filter({hasText:'P001'}).getByRole('button',{name:'Review now',exact:true}).click();
  await expect(page.locator('[data-item-date]')).toHaveCount(2);
  await page.locator('#select-date-items').check();await page.locator('#bulk-date').fill('2026-02-02');await page.locator('#apply-dates').click();
  await expect(page.locator('[data-item-date="0"]')).toHaveValue('2026-02-02');await expect(page.locator('[data-item-date="1"]')).toHaveValue('2026-02-02');
  await page.locator('[data-item-date="0"]').fill('');await page.locator('[data-item-date="0"]').blur();
  await expect(page.locator('[data-item-date="0"]')).toHaveValue('');await expect(page.locator('[data-item-date="1"]')).toHaveValue('2026-02-02');
  await page.locator('#save-now').click();await expect(page.locator('#save-status')).toHaveText('All changes saved');
  await page.screenshot({path:path.join(root,'work/date-review-owner.png')});
  await page.locator('#check-current').click();await page.locator('#save-now').click();await page.locator('#submit').click();
  await expect.poll(async()=>await page.locator('#last-submitted').count()||await page.locator('#last-review').count()).toBeTruthy();
  console.log('Owner review: two-food bulk date, isolated Unknown date, save and submit passed');
 }finally{await app.close();}
 ({app,page}=await launch('date-offline'));
 try{
  await choose(app,qa.packages[0].path);await page.getByRole('button',{name:'Review a package',exact:true}).click();
  await page.getByRole('button',{name:'Open',exact:true}).click();
  await page.locator('[data-item-date="0"]').fill('2026-03-03');await page.locator('[data-item-date="0"]').blur();
  await page.locator('#save-now').click();await expect(page.locator('#save-status')).toHaveText('All changes saved');
  await page.locator('#check-current').click();await page.locator('#save-now').click();await page.locator('#submit').click();await page.locator('#last-review').click();
  await expect(page.locator('[data-item-date="0"]')).toHaveValue('2026-03-03');
  await page.getByRole('button',{name:'← Queue',exact:true}).click();await expect(page.getByRole('button',{name:'Download results to return',exact:true})).toBeEnabled();
  console.log('Offline review: date correction persists through submission and result export is enabled');
 }finally{await app.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
