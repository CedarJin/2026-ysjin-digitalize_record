'use strict';
const {_electron:electron,expect}=require('playwright/test');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),{execFileSync}=require('node:child_process');
const root=path.resolve(__dirname,'..');
execFileSync(path.join(root,'.venv/bin/python'),[path.join(root,'scripts/desktop/prepare_qa.py')],{stdio:'ignore'});
const qa=JSON.parse(fs.readFileSync(path.join(root,'work/desktop-qa-manifest.json')));
const folder=path.join(qa.folder,'Sync inputs');fs.mkdirSync(folder);fs.copyFileSync(path.join(qa.folder,'New scan.pdf'),path.join(folder,'R003.pdf'));fs.writeFileSync(path.join(folder,'metadata.csv'),'record_id,participant,date\nR003,P003,2026-01-03\n');
async function launch(){const app=await electron.launch({executablePath:path.join(root,'dist/macos/Food Record-darwin-arm64/Food Record.app/Contents/MacOS/Food Record'),args:['--user-data-dir='+path.join(qa.folder,'sync-owner')],env:{...process.env,PATH:'/usr/bin:/bin'}});const page=await app.firstWindow();page.setDefaultTimeout(20000);await page.getByRole('button',{name:'Continue a study',exact:true}).waitFor({timeout:60000});await app.evaluate(({dialog},file)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[file]});},qa.study);await page.getByRole('button',{name:'Continue a study',exact:true}).click();return {app,page};}
(async()=>{let {app,page}=await launch();try{
 await app.evaluate(({dialog},folder)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[folder]});},folder);
 await page.getByRole('button',{name:'Sync folder',exact:true}).click();
 await expect(page.locator('#import-report')).toContainText('1 new or updated records');
 await expect(page.locator('#toggle-sync-files')).toHaveText('Show files and metadata');
 await page.screenshot({path:path.join(root,'work/sync-folder-preview.png')});
 await page.getByRole('button',{name:'Import changes',exact:true}).click();await expect(page.locator('#rows')).toContainText('R003');
 await app.evaluate(({dialog})=>{dialog.showOpenDialog=async()=>{throw Error('The saved sync folder should not need a picker');};});
 await page.getByRole('button',{name:'Sync folder',exact:true}).click();await expect(page.locator('#import-report')).toContainText('0 new or updated records');
 await page.getByRole('button',{name:'Finish sync — no changes',exact:true}).click();await expect(page.locator('#rows')).toContainText('R003');
 fs.copyFileSync(path.join(qa.folder,'New scan.pdf'),path.join(folder,'R004.pdf'));fs.appendFileSync(path.join(folder,'metadata.csv'),'R004,P004,2026-01-04\n');
 await page.getByRole('button',{name:'Sync folder',exact:true}).click();await expect(page.locator('#import-report')).toContainText('1 new or updated records');await expect(page.locator('#import-report')).toContainText('1 unchanged');
 await page.getByRole('button',{name:'Import changes',exact:true}).click();await expect(page.locator('#rows')).toContainText('R004');
 const state=await page.evaluate(async()=>await (await fetch('/desktop/state')).json());assert.equal(state.queue.active,0);assert.equal(state.queue.queued,0);assert.equal(state.records.length,4);
 console.log('Folder selection, automatic preview, no-change sync and incremental PDF/metadata import passed');
}finally{await app.close();}
({app,page}=await launch());try{
 await app.evaluate(({dialog})=>{dialog.showOpenDialog=async()=>{throw Error('Remembered folder was lost on restart');};});
 await page.getByRole('button',{name:'Sync folder',exact:true}).click();await expect(page.locator('#import-report')).toContainText('0 new or updated records');await page.getByRole('button',{name:'Finish sync — no changes',exact:true}).click();await expect(page.locator('#rows')).toContainText('R003');
 await page.getByRole('button',{name:'Settings',exact:true}).click();await expect(page.locator('#dialog')).toContainText(folder);await expect(page.getByRole('button',{name:'Change sync folder',exact:true})).toBeVisible();await page.getByRole('button',{name:'Close',exact:true}).click();
 console.log('Sync folder remembered after app restart; change-folder control available; no AI processing started');
}finally{await app.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
