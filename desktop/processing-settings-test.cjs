'use strict';
const {_electron:electron,expect}=require('playwright/test');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {execFileSync}=require('node:child_process');const root=path.resolve(__dirname,'..');
(async()=>{
 execFileSync(path.join(root,'.venv/bin/python'),[path.join(root,'scripts/desktop/prepare_qa.py')],{stdio:'ignore'});
 const qa=JSON.parse(fs.readFileSync(path.join(root,'work/desktop-qa-manifest.json')));
 execFileSync(path.join(root,'.venv/bin/python'),['-c',`import sys\nfrom pathlib import Path\nsys.path.insert(0,'tests')\nfrom test_day_review import add_source\nfrom food_record_digitizer.workspace import Workspace\nadd_source(Workspace(Path(sys.argv[1])),Path(sys.argv[2]),rid='R003',sid='P003',candidate=False)`,qa.study,qa.folder],{cwd:root});
 execFileSync(path.join(root,'.venv/bin/python'),['-c',`import sys\nfrom pathlib import Path\nfrom sqlalchemy import select\nfrom food_record_digitizer.workspace import Workspace\nfrom food_record_digitizer.db import Record,ExtractionRun\nw=Workspace(Path(sys.argv[1]))\nwith w.factory.begin() as s:\n r=s.scalar(select(Record).where(Record.record_id=='R003'))\n s.add(ExtractionRun(record_pk=r.id,input_version=r.input_version,cache_key='synthetic-host-error',status='failed',error_code='api_http_401',configuration={'failure_diagnostic':{'provider_code':'incorrect_hostname','required_base_url':'https://us.api.openai.com/v1'}}))`,qa.study],{cwd:root});
 const profile=path.join(qa.folder,'settings-profile'),fake='synthetic-settings-key-not-a-real-credential';
 const app=await electron.launch({executablePath:path.join(root,'dist/macos/Food Record-darwin-arm64/Food Record.app/Contents/MacOS/Food Record'),args:['--user-data-dir='+profile],env:{...process.env,PATH:'/usr/bin:/bin'}});
 let page;
 try{
  page=await app.firstWindow();page.setDefaultTimeout(20000);
  await page.getByRole('button',{name:'Continue a study',exact:true}).waitFor({timeout:60000});
  await app.evaluate(({dialog},study)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[study]});},qa.study);
  await page.getByRole('button',{name:'Continue a study',exact:true}).click();
  const failedRow=page.locator('tr').filter({has:page.getByRole('checkbox',{name:'Select R003',exact:true})});await failedRow.getByText('Details',{exact:true}).click();await expect(failedRow).toContainText('OpenAI requires the regional endpoint https://us.api.openai.com/v1');
  await page.getByRole('checkbox',{name:'Select R003',exact:true}).check();await page.getByRole('button',{name:'Process selected',exact:true}).click();
  await page.getByRole('button',{name:'Start processing',exact:true}).click();
  await expect(page.locator('#dialog-error')).toContainText('Add your API key');
  await expect(page.locator('#dialog-error')).toBeVisible();
  await expect(page.getByRole('button',{name:'Start processing',exact:true})).toBeEnabled();
  await page.locator('#close-dialog').click();await page.getByRole('button',{name:'Settings',exact:true}).click();
  await expect(page.locator('#key-status')).toContainText('not configured');
  await expect(page.locator('#dialog')).toContainText('You do not need a new API key.');
  await page.locator('#base_url').selectOption('https://us.api.openai.com/v1');
  await page.locator('#api-key').fill(fake);await page.getByRole('button',{name:'Save settings',exact:true}).click();
  await expect(page.locator('#dialog')).not.toBeVisible();
  await expect.poll(()=>page.evaluate(()=>fetch('/desktop/state').then(r=>r.json()).then(s=>s.has_key))).toBe(true);
  const encrypted=fs.readFileSync(path.join(profile,'api-key.enc'));assert(!encrypted.includes(Buffer.from(fake)));
  await page.getByRole('button',{name:'Settings',exact:true}).click();await expect(page.locator('#key-status')).toContainText('API key: saved');
  await app.evaluate(({ipcMain})=>{ipcMain.removeHandler('save-key');ipcMain.handle('save-key',()=>({error:'Synthetic Keychain failure'}));});
  await page.locator('#api-key').fill('synthetic-replacement');await page.getByRole('button',{name:'Save settings',exact:true}).click();
  await expect(page.locator('#dialog-error')).toHaveText('Synthetic Keychain failure');await expect(page.locator('#dialog')).toBeVisible();
  await expect(page.locator('#save-settings')).toBeEnabled();
  const state=await page.evaluate(()=>fetch('/desktop/state').then(r=>r.json()));assert.equal(state.queue.active,0);assert.equal(state.queue.queued,0);
  await page.screenshot({path:path.join(root,'work/processing-settings-inline-error.png')});
  await page.locator('#api-key').fill('');await page.locator('#close-dialog').click();
  console.log('Regional-host diagnostic and required US endpoint visible in Settings; Missing-key start failure visible inside dialog; Save settings saves a new key encrypted and updates backend; Keychain failure stays visible with retry; no API requests');
 }catch(error){if(page){await page.screenshot({path:path.join(root,'work/processing-settings-failure.png')}).catch(()=>{});}throw error;}
 finally{await app.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
