'use strict';
const {_electron:electron,expect}=require('playwright/test');
const fs=require('node:fs'),path=require('node:path'),{execFileSync}=require('node:child_process');
const root=path.resolve(__dirname,'..');
execFileSync(path.join(root,'.venv/bin/python'),[path.join(root,'scripts/desktop/prepare_qa.py')],{stdio:'ignore'});
const qa=JSON.parse(fs.readFileSync(path.join(root,'work/desktop-qa-manifest.json')));
execFileSync(path.join(root,'.venv/bin/python'),['-c',`import copy,sys\nfrom sqlalchemy import select\nfrom food_record_digitizer.workspace import Workspace\nfrom food_record_digitizer import db\nw=Workspace(sys.argv[1])\nwith w.factory.begin() as s:\n r=s.scalar(select(db.Record).where(db.Record.record_id=='R001'))\n c=s.scalar(select(db.Candidate).where(db.Candidate.record_pk==r.id))\n data=copy.deepcopy(c.content)\n food=data['items'][0];food['food_name']=copy.deepcopy(food['description']);food['food_name']['normalized']='Banana bread'\n item=copy.deepcopy(food);item['item_id']='flour';item['food_name']['normalized']='Flour';item['description']['normalized']='Whole wheat';item['amount']=copy.deepcopy(item['description']);item['amount']['normalized']='200';item['unit']=copy.deepcopy(item['description']);item['unit']['normalized']='g';item['quantity']=200;data['items'].append(item);s.add(db.Candidate(record_pk=r.id,input_version=r.input_version,content=data,provenance=c.provenance))\n`,qa.study]);
async function launch(profile){const app=await electron.launch({executablePath:path.join(root,'dist/macos/Food Record-darwin-arm64/Food Record.app/Contents/MacOS/Food Record'),args:['--user-data-dir='+path.join(qa.folder,profile)],env:{...process.env,PATH:'/usr/bin:/bin'}});const page=await app.firstWindow();page.setDefaultTimeout(20000);await page.getByRole('button',{name:'Continue a study',exact:true}).waitFor({timeout:60000});return {app,page};}
async function choose(app,file){await app.evaluate(({dialog},file)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[file]});},file);}
(async()=>{
 let {app,page}=await launch('recipe-owner');
 try{
  await choose(app,qa.study);await page.getByRole('button',{name:'Continue a study',exact:true}).click();
  await page.getByRole('link',{name:'Review days',exact:true}).click();
  await page.locator('tr').filter({hasText:'P001'}).getByRole('button',{name:/^(Review|Continue review)$/,exact:true}).click();
  await expect(page.locator('details.item')).toHaveCount(2);
  const rid=await page.evaluate(()=>S.detail.record_id);
  await page.locator('details.item').nth(1).locator('.edit-hint').click();await page.locator('[data-move-recipe="1"]').click();
  await page.locator('#ingredients-confirm').click();
  await expect(page.locator('details.item')).toHaveCount(1);await expect(page.locator('.ingredient')).toHaveCount(1);await page.locator('details.item').first().evaluate(el=>el.open=true);await page.locator('[data-basis="0"]').selectOption('whole_recipe');
  await page.locator('[data-value="items.0.ingredients.0.amount"]').fill('250');
  await page.locator('#save-now').click();await expect(page.locator('#save-status')).toHaveText('All changes saved');
  await page.reload();await page.locator('tr').filter({hasText:'P001'}).getByRole('button',{name:/^(Review|Continue review)$/,exact:true}).click();
  await expect(page.locator('[data-value="items.0.ingredients.0.amount"]')).toHaveValue('250');
  await page.locator('details.item').first().evaluate(el=>el.open=true);await page.locator('[data-separate-ingredient="0,0"]').click();
  await expect(page.locator('details.item')).toHaveCount(2);await expect(page.locator('[data-value="items.1.amount"]').first()).toHaveValue('');
  await page.locator('[data-confirm-food="1"]').click();
  await page.locator('[data-action="add-item"]').click();await expect(page.locator('#ask-response')).toHaveCount(0);await expect(page.locator('details.item')).toHaveCount(3);
  await page.locator('[data-delete="2"]').click();await expect(page.locator('#ask-response')).toHaveCount(0);await page.getByRole('button',{name:'Cancel',exact:true}).click();await expect(page.locator('details.item')).toHaveCount(3);
  await page.locator('[data-delete="2"]').click();await page.getByRole('button',{name:'Continue',exact:true}).click();await expect(page.locator('details.item')).toHaveCount(2);
  await page.locator('[data-date-select="1"]').check();await page.locator('#make-ingredients').click();await page.locator('#ingredients-confirm').click();
  await page.locator('[data-value="items.0.ingredients.0.amount"]').fill('250');await page.locator('[data-value="items.0.ingredients.0.unit"]').fill('g');
  await page.locator('#save-now').click();await expect(page.locator('#save-status')).toHaveText('All changes saved');
  await page.locator('.recipe-panel').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(root,'work/recipe-review-owner.png')});
  await page.locator('#check-current').click();await page.locator('#save-now').click();await page.locator('#submit').click();
  await expect.poll(async()=>await page.locator('#last-submitted').count()||await page.locator('#last-review').count()).toBeTruthy();
  const result=await page.evaluate(async rid=>{const approval=await api('/approve',{record_ids:[rid]});const r=await api('/releases',{record_ids:[rid]});const response=await fetch('/desktop/export/release/'+r.id+'?format=csv');return {approval,csv:await response.text(),status:response.status};},rid);
  if(result.status!==200||!result.csv.includes('Banana bread')||!result.csv.includes('whole recipe'))throw Error(JSON.stringify(result));
  fs.writeFileSync(path.join(root,'work/recipe-example.csv'),result.csv);
  console.log('Owner app: ingredient move, edit, reload, separation, approval and simple CSV download passed');
 }finally{await app.close();}
 ({app,page}=await launch('recipe-offline'));
 try{
  await choose(app,qa.packages[0].path);await page.getByRole('button',{name:'Review a package',exact:true}).click();await page.getByRole('button',{name:'Open',exact:true}).click();
  await page.locator('details.item .edit-hint').click();await page.locator('.recipe-panel > summary').click();await page.locator('[data-add-ingredient="0"]').click();await expect(page.locator('#ask-response')).toHaveCount(0);
  await page.locator('[data-value="items.0.ingredients.0.food_name"]').fill('Walnuts');await page.locator('[data-basis="0"]').selectOption('whole_recipe');
  await page.locator('#save-now').click();await expect(page.locator('#save-status')).toHaveText('All changes saved');
  await page.locator('#check-current').click();await page.locator('#save-now').click();await page.locator('#submit').click();await page.locator('#last-review').click();
  await expect(page.locator('[data-value="items.0.ingredients.0.food_name"]')).toHaveValue('Walnuts');
  await page.getByRole('button',{name:'← Queue',exact:true}).click();await expect(page.getByRole('button',{name:'Download results to return',exact:true})).toBeEnabled();
  console.log('Offline app: ingredient add, save, submit and return export readiness passed');
 }finally{await app.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
