const {_electron:electron,expect}=require('playwright/test');
const fs=require('node:fs');const path=require('node:path');
const root=path.resolve(__dirname,'..');
require('node:child_process').execFileSync(path.join(root,'.venv/bin/python'),[path.join(root,'scripts/desktop/prepare_qa.py')],{stdio:'ignore'});
const qa=JSON.parse(fs.readFileSync(path.join(root,'work/desktop-qa-manifest.json')));
const executablePath=process.env.FOOD_RECORD_TEST_APP||path.join(root,'dist/macos/Food Record-darwin-arm64/Food Record.app/Contents/MacOS/Food Record');
async function launch(profile){const app=await electron.launch({executablePath,args:['--user-data-dir='+path.join(qa.folder,profile)],env:{...process.env,PATH:'/usr/bin:/bin'}});const page=await app.firstWindow();page.setDefaultTimeout(15000);await page.getByRole('button',{name:'Continue a study',exact:true}).waitFor({timeout:60000});return {app,page};}
async function choose(app,target){await app.evaluate(({dialog},target)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[target]});},target);}
async function submit(page){
 await page.locator('#check-current').click();await page.locator('#save-now').click();await page.locator('#submit').click();
 await expect.poll(async()=>await page.locator('#last-submitted').count()||await page.locator('#last-review').count()).toBeTruthy();
 if(await page.locator('#last-submitted').count())await page.locator('#last-submitted').click();else await page.locator('#last-review').click();
 await page.getByRole('button',{name:'Edit review',exact:true}).waitFor();
}

(async()=>{
 let {app,page}=await launch('owner-profile');const errors=[];page.on('pageerror',e=>errors.push(e.message));
 try{
  await choose(app,qa.study);await page.getByRole('button',{name:'Continue a study',exact:true}).click();await page.getByRole('heading',{name:'Desktop workflow test'}).waitFor({timeout:60000});
  await page.getByRole('button',{name:'Add records',exact:true}).click();
  await page.locator('#pdfs').setInputFiles([path.join(qa.folder,'New scan.pdf'),path.join(qa.folder,'Attachment.pdf')]);
  await page.locator('#metadata').setInputFiles(path.join(qa.folder,'metadata.csv'));
  await page.getByRole('button',{name:'Continue to associations'}).click();
  await page.locator('[data-source="0"]').fill('R003');await page.locator('[data-source="1"]').fill('R003-A1');
  await page.getByRole('button',{name:'Check import',exact:true}).click();
  await page.getByRole('button',{name:'Import records',exact:true}).click();
  await page.getByRole('heading',{name:'Desktop workflow test'}).waitFor();
  console.log('Editable batch association + unknown date + missing PDF import passed');
  await page.getByRole('checkbox',{name:'Select R001',exact:true}).check();await page.getByRole('button',{name:'Process selected'}).click();
  await page.getByText('1 existing drafts will be reused',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Reuse existing results'}).click();console.log('Existing draft reused without key / API');
  await page.getByRole('link',{name:'Review',exact:true}).click();
  await page.locator('tr').filter({hasText:'P001'}).getByRole('button',{name:'Review now',exact:true}).click();
  await page.getByRole('button',{name:'Submit & next',exact:true}).waitFor();
  await page.getByRole('textbox',{name:'Time normalized',exact:true}).fill('08:30');await page.locator('#save-now').click();
  await page.locator('#scan').waitFor();await page.waitForFunction(()=>document.querySelector('#scan').naturalWidth>0);
  await page.screenshot({path:path.join(root,'work/desktop-review-qa.png')});
  await submit(page);await page.getByRole('button',{name:'Approve for research'}).click();
  // Approval dialog (the existing review app uses an explicit confirmation).
  if(await page.locator('dialog [data-accept]').count())await page.locator('dialog [data-accept]').click();
  await page.getByText('Approved',{exact:true}).first().waitFor();if(await page.locator('#modal[open]').count())await page.locator('#close-modal').click();
  await page.getByRole('button',{name:'Edit review',exact:true}).click();await page.locator('dialog [data-accept]').click();
  await page.getByRole('button',{name:'Submit & next',exact:true}).waitFor();
  console.log('PDF rendering + owner submission + approval + reopen passed');
 }catch(e){await page.screenshot({path:path.join(root,'work/desktop-workflow-failure.png')});console.log(await page.locator('body').innerText());throw e;}finally{await app.close();}
 ({app,page}=await launch('reviewer-profile'));
 try{
  await choose(app,qa.packages[0].path);await page.getByRole('button',{name:'Review a package',exact:true}).click();
  await page.getByRole('heading',{name:'Review package',exact:true}).waitFor({timeout:60000});
  await page.getByRole('button',{name:'Open',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#scan').naturalWidth>0);
  await submit(page);await page.getByRole('button',{name:'Edit review',exact:true}).click();await page.getByRole('button',{name:'Submit & next'}).waitFor();
  await page.locator('#submit').click();await page.locator('#last-review').click();await page.getByRole('button',{name:'Edit review'}).waitFor();
  await page.getByRole('button',{name:'← Queue',exact:true}).click();
  const dest=path.join(qa.folder,'alice-results.zip');
  await app.evaluate(({session},dest)=>{session.defaultSession.on('will-download',(_e,item)=>item.setSavePath(dest));},dest);
  await page.getByRole('button',{name:'Download results to return',exact:true}).click();await expect.poll(()=>fs.existsSync(dest)&&fs.statSync(dest).size>0).toBeTruthy();
  await page.screenshot({path:path.join(root,'work/desktop-offline-qa.png')});
  console.log('Reviewer without key: open + submit + reopen + resubmit + export passed');
 }catch(e){await page.screenshot({path:path.join(root,'work/desktop-workflow-failure.png')});console.log(await page.locator('body').innerText());throw e;}finally{await app.close();}
 if(errors.length)throw Error(errors.join('\n'));
 console.log('Workflow GUI checks passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
