'use strict';
const {_electron:electron,expect}=require('playwright/test'),fs=require('node:fs'),path=require('node:path'),{execFileSync}=require('node:child_process');
const root=path.resolve(__dirname,'..');
execFileSync(path.join(root,'.venv/bin/python'),[path.join(root,'scripts/desktop/prepare_qa.py')],{stdio:'ignore'});
const qa=JSON.parse(fs.readFileSync(path.join(root,'work/desktop-qa-manifest.json')));
(async()=>{
 const app=await electron.launch({executablePath:path.join(root,'dist/macos/Food Record-darwin-arm64/Food Record.app/Contents/MacOS/Food Record'),args:['--user-data-dir='+path.join(qa.folder,'delete-package')],env:{...process.env,PATH:'/usr/bin:/bin'}});
 try{
 const page=await app.firstWindow();page.setDefaultTimeout(20000);
 await page.getByRole('button',{name:'Continue a study',exact:true}).waitFor({timeout:60000});
 await app.evaluate(({dialog},file)=>dialog.showOpenDialog=async()=>({canceled:false,filePaths:[file]}),qa.study);
 await page.getByRole('button',{name:'Continue a study',exact:true}).click();await page.locator('#select-all').check();await page.locator('#review-selected').click();await page.locator('#back').click();
 await page.getByText('Study tools',{exact:true}).click();await page.locator('[data-nav=offline]').click();
 const before=await page.evaluate(()=>fetch('/api/offline/packages').then(r=>r.json()));expect(before.length).toBeGreaterThan(0);const id=before[0].id;
 await page.locator(`[data-delete-package="${id}"]`).click();await page.locator('[data-cancel]').click();await expect(page.locator(`[data-delete-package="${id}"]`)).toBeVisible();
 await page.locator(`[data-delete-package="${id}"]`).click();await page.locator('[data-accept]').click();await expect(page.locator(`[data-delete-package="${id}"]`)).toHaveCount(0);
 const status=await page.evaluate(id=>fetch('/api/offline/packages/'+id+'/download').then(r=>r.status),id);expect(status).toBe(410);
 await page.reload();await page.getByText('Study tools',{exact:true}).click();await page.locator('[data-nav=offline]').click();await expect(page.locator(`[data-delete-package="${id}"]`)).toHaveCount(0);
 console.log('Package deletion: cancel preserves package, confirm removes it, reload keeps it hidden and old download is blocked.');
 }finally{await app.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
