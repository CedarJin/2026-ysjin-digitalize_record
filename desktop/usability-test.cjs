const path=require('node:path'),fs=require('node:fs'),{execFileSync}=require('node:child_process');
const root=process.cwd(),{_electron:electron,expect}=require(path.join(root,'desktop/node_modules/playwright/test'));
(async()=>{
 execFileSync('.venv/bin/python',['scripts/desktop/prepare_qa.py'],{stdio:'ignore'});
 const qa=JSON.parse(fs.readFileSync('work/desktop-qa-manifest.json'));
 const app=await electron.launch({executablePath:path.join(root,'dist/macos/Food Record-darwin-arm64/Food Record.app/Contents/MacOS/Food Record'),args:['--user-data-dir='+path.join(qa.folder,'usability')],env:{...process.env,PATH:'/usr/bin:/bin'}});
 try{
 const page=await app.firstWindow();page.setDefaultTimeout(20000);const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.getByRole('button',{name:'Continue a study',exact:true}).waitFor({timeout:60000});
 await app.evaluate(({dialog},file)=>dialog.showOpenDialog=async()=>({canceled:false,filePaths:[file]}),qa.study);
 await page.getByRole('button',{name:'Continue a study',exact:true}).click();
 await page.locator('#queue-nav').click();await expect(page.locator('#main')).toContainText('No processing tasks');await expect(page.locator('#main')).not.toContainText('Processing is complete');await page.locator('#queue-records').click();
 const poll=page.waitForResponse(r=>new URL(r.url()).pathname==='/desktop/state');await page.locator('#search').fill('P001');await poll;await expect(page.locator('#search')).toBeFocused();await expect(page.locator('#search')).toHaveValue('P001');
 await page.locator('[data-select="R001"]').check();await page.locator('#review-selected').click();
 await page.locator('#back').waitFor().catch(async e=>{console.log('PAGE',page.url(),await page.locator('body').innerText(),'ERRORS',errors);await page.screenshot({path:'work/usability-failure.png'});throw e;});await expect(page.locator('details.item')).toHaveCount(1);
 await expect(page.getByRole('textbox',{name:'Food for food 1',exact:true})).toBeVisible();
 await page.evaluate(()=>{window.qaIssues=S.content.issues;S.content.issues=[0,1].map(()=>({code:'check_preparation',target:'record.items[0].preparation',message:'Check preparation against the scan.',severity:'warning',resolution:null}));});
 await page.getByRole('button',{name:'Issues',exact:true}).click();await expect(page.locator('#editor .issue')).toHaveCount(1);await expect(page.locator('#editor')).not.toContainText('record.items[');await page.locator('[data-issue-item="0"]').click();await expect(page.locator('details.item').first()).toHaveAttribute('open','');
 await page.evaluate(()=>{S.content.issues=window.qaIssues;renderEditor();});

 await page.getByRole('textbox',{name:'Time for food 1',exact:true}).fill('08:30');await page.locator('#save-now').click();await expect(page.locator('#save-status')).toHaveText('All changes saved');
 await page.screenshot({path:'work/usability-compact-editor.png'});
 await page.locator('#back').click();await expect(page.getByRole('heading',{name:'Selected review days',exact:true})).toBeVisible();
 await expect(page.locator('[data-pick]')).toHaveCount(1);await expect(page.locator('[data-pick]').first()).toBeChecked();
 await page.getByRole('link',{name:'Work list',exact:true}).click();await expect(page.locator('[data-select="R001"]')).toBeChecked();
 await page.locator('#export-nav').click();await expect(page.locator('#main')).toContainText('1 records selected');await expect(page.locator('#release-selected')).toBeEnabled();
 await page.locator('#change-download-selection').click();await page.getByRole('link',{name:'Review days',exact:true}).click();
 await page.locator('#search').fill('P001');await page.locator('#batch-offline').click();
 await page.getByText('New reviewer? Add name and email',{exact:true}).click();await page.locator('#offline-add-reviewer [name=name]').fill('Charlie Example');await page.locator('#offline-add-reviewer [name=email]').fill('charlie@example.test');await page.locator('#offline-add-reviewer button').click();await expect(page.locator('#offline-add-status')).toContainText('Reviewer added');
 await page.getByText('Select unassigned days',{exact:true}).click();await page.locator('#send-independent').check();await page.locator('#send-next').click();await expect(page.locator('#send-selection')).toContainText('1 records · 1 review days selected');
 await page.locator('#offline-create').click();await expect(page.getByRole('heading',{name:'Ready to send',exact:true})).toBeVisible();await expect(page.locator('#modal-body')).toContainText('1 days · 1 packages');
 await page.locator('#close-modal').click();await page.locator('#batch-offline').click();await page.locator('#offline-reviewer').selectOption({label:'Charlie Example (charlie@example.test)'});await page.locator('#offline-create').click();await expect(page.locator('#modal-body')).toContainText('Existing package reused');
 await page.locator('#close-modal').click();await page.locator('#search').fill('P001');await page.locator('#filter').selectOption('all');
 await page.getByRole('link',{name:'Work list',exact:true}).click();await page.getByRole('link',{name:'Review days',exact:true}).click();await expect(page.locator('#search')).toHaveValue('P001');await expect(page.locator('#filter')).toHaveValue('all');
 await page.route('**/api/records?*',route=>route.fulfill({status:503,json:{error:'Synthetic temporary failure'}}));await page.reload();await expect(page.getByRole('heading',{name:'Unable to load the review list',exact:true})).toBeVisible();await expect(page.locator('#auth')).toBeHidden();await page.unroute('**/api/records?*');await page.locator('#retry-review-load').click();await expect(page.locator('#search')).toHaveValue('P001');
 if(errors.length)throw Error(errors.join('\n'));
 console.log('Compact editing/save, selected review scope, return selection, export scope and idempotent send passed.');
 }finally{await app.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
