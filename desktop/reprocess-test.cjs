'use strict';
const {_electron:electron,expect}=require('playwright/test');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),{execFileSync}=require('node:child_process');
const root=path.resolve(__dirname,'..');
execFileSync(path.join(root,'.venv/bin/python'),[path.join(root,'scripts/desktop/prepare_qa.py')],{stdio:'ignore'});
const qa=JSON.parse(fs.readFileSync(path.join(root,'work/desktop-qa-manifest.json')));
(async()=>{const app=await electron.launch({executablePath:path.join(root,'dist/macos/Food Record-darwin-arm64/Food Record.app/Contents/MacOS/Food Record'),args:['--user-data-dir='+path.join(qa.folder,'reprocess-qa')],env:{...process.env,PATH:'/usr/bin:/bin'}});try{
 const page=await app.firstWindow();page.setDefaultTimeout(20000);
 await page.getByRole('button',{name:'Continue a study',exact:true}).waitFor({timeout:60000});
 await app.evaluate(({dialog},file)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[file]});},qa.study);
 await page.getByRole('button',{name:'Continue a study',exact:true}).click();
 let requests=[];
 // Synthetic UI test only. Never forward processing requests to an API.
 await page.route('**/desktop/process/start',async route=>{requests.push(route.request().postDataJSON());await route.fulfill({json:{requests:1}});});
 await page.locator('[data-reprocess="R001"]').click();
 await expect(page.locator('#dialog')).toContainText('1 records require requests');
 await expect(page.locator('#dialog')).toContainText('may incur fees');
 assert.equal(requests.length,0);
 await page.screenshot({path:path.join(root,'work/reprocess-preview.png')});
 await page.locator('#start').click();await expect.poll(()=>requests.length).toBe(1);
 assert.equal(requests[0].force,true);assert.deepEqual(requests[0].ids,['R001']);assert.ok(requests[0].processing_tokens.R001);
 await page.locator('[data-select="R001"]').check();await page.locator('[data-select="R002"]').check();await page.locator('#process').click();
 await expect(page.locator('#dialog')).toContainText('2 existing AI drafts will be reused');
 await page.locator('#reprocess-selected').click();await expect(page.locator('#dialog')).toContainText('2 records require requests');
 await page.locator('#start').click();await expect.poll(()=>requests.length).toBe(2);
 assert.equal(requests[1].force,true);assert.deepEqual(requests[1].ids.sort(),['R001','R002']);
 console.log('Single and batch re-process preview/confirmation passed; no real API requests sent');
}finally{await app.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
