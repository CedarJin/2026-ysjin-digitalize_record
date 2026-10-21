'use strict';
const {_electron:electron,expect}=require('playwright/test');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {execFileSync}=require('node:child_process');const root=path.resolve(__dirname,'..');
(async()=>{
 execFileSync(path.join(root,'.venv/bin/python'),[path.join(root,'scripts/desktop/prepare_qa.py')],{stdio:'ignore'});
 const qa=JSON.parse(fs.readFileSync(path.join(root,'work/desktop-qa-manifest.json')));
 const study=path.join(qa.folder,'Organized study'),incoming=path.join(qa.folder,'PDF scans');fs.mkdirSync(incoming);
 execFileSync(path.join(root,'.venv/bin/python'),['-c',`import sys\nfrom pathlib import Path\nsys.path.insert(0,'tests')\nfrom test_ingest import pdf\np=Path(sys.argv[1]);pdf(p/'R010.pdf');pdf(p/'R011.pdf',2)`,incoming],{cwd:root});
 const metadata=path.join(qa.folder,'separate-metadata.csv');fs.writeFileSync(metadata,'record_id,participant,date\nR010,P10,\nR011,P11,2026-10-21\n');
 const app=await electron.launch({executablePath:path.join(root,'dist/macos/Food Record-darwin-arm64/Food Record.app/Contents/MacOS/Food Record'),args:['--user-data-dir='+path.join(qa.folder,'layout-profile')],env:{...process.env,PATH:'/usr/bin:/bin'}});
 let page;
 try{
  page=await app.firstWindow();page.setDefaultTimeout(20000);
  await page.getByRole('button',{name:'Start a new study',exact:true}).waitFor({timeout:60000});
  await app.evaluate(({dialog},filePath)=>{dialog.showSaveDialog=async()=>({canceled:false,filePath});},study);
  await page.getByRole('button',{name:'Start a new study',exact:true}).click();
  await page.getByRole('button',{name:'Choose PDF folder',exact:true}).waitFor({timeout:60000});
  await expect(page.getByRole('button',{name:'Home',exact:true})).toBeVisible();
  const chooserReady=page.waitForEvent('filechooser');await page.getByRole('button',{name:'Choose PDF folder',exact:true}).click();await (await chooserReady).setFiles(incoming);
  await page.locator('#metadata').setInputFiles(metadata);
  await page.getByRole('button',{name:'Continue to associations',exact:true}).click();
  await expect(page.locator('[data-source]')).toHaveCount(2);assert.deepEqual((await page.locator('[data-source]').evaluateAll(els=>els.map(el=>el.value))).sort(),['R010','R011']);
  await page.getByRole('button',{name:'Choose different files',exact:true}).click();
  await expect(page.getByRole('button',{name:'Choose PDF folder',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Records',exact:true}).click();await page.getByRole('button',{name:'Resume unfinished import',exact:true}).click();
  await page.getByRole('button',{name:'Check import',exact:true}).click();await page.getByRole('button',{name:'Import records',exact:true}).click();
  await page.locator('[data-edit-metadata="R010"]').waitFor();
  assert(fs.existsSync(path.join(study,'data/sources/records/R010/R010.pdf')));
  assert.deepEqual(fs.readdirSync(study).sort(),['.app','backups','data','results','workspace.json']);
  let state=await page.evaluate(()=>fetch('/desktop/state').then(r=>r.json()));assert.equal(state.path,study);assert.equal(state.records.length,2);
  await page.getByRole('button',{name:'Home',exact:true}).click();
  await app.evaluate(({dialog},filePath)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[filePath]});},qa.study);
  await page.getByRole('button',{name:'Continue a study',exact:true}).click();await page.locator('[data-edit-metadata="R001"]').waitFor();
  const copy=path.join(qa.folder,'Organized legacy copy');await app.evaluate(({dialog},filePath)=>{dialog.showSaveDialog=async()=>({canceled:false,filePath});},copy);
  await page.getByRole('button',{name:'Settings',exact:true}).click();await page.getByText('Study folder',{exact:true}).click();const originalModel=await page.locator('#model').inputValue();await page.locator('#model').fill('unsaved-model-change');await page.getByRole('button',{name:'Save organized copy',exact:true}).click();await expect(page.locator('#dialog-error')).toContainText('Save settings before');assert(!fs.existsSync(copy));await page.locator('#model').fill(originalModel);await page.getByRole('button',{name:'Save organized copy',exact:true}).click();
  await page.getByText(copy,{exact:true}).waitFor({timeout:60000});
  state=await page.evaluate(()=>fetch('/desktop/state').then(r=>r.json()));assert.equal(state.records.length,2);assert.equal(state.queue.active,0);assert.equal(state.queue.queued,0);
  assert(fs.existsSync(path.join(qa.study,'records.db')));assert(!fs.existsSync(path.join(qa.study,'.app')));assert(fs.existsSync(path.join(copy,'.app/records.db')));
  assert.equal(fs.readlinkSync(path.join(copy,'.app/sources')),'../data/sources');
  await page.screenshot({path:path.join(root,'work/organized-study-gui.png')});
  console.log('PDF folder plus separate metadata, back to selection with saved draft, Home navigation, organized new study, non-destructive organized legacy copy passed; no AI requests');
 }catch(error){if(page){await page.screenshot({path:path.join(root,'work/organized-study-gui-failure.png')}).catch(()=>{});console.log((await page.locator('body').innerText()).slice(0,4500));}throw error;}
 finally{await app.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
