'use strict';
// Tests the distributed ZIP while both original build output directories are absent.
const {_electron:electron}=require('playwright');
const fs=require('node:fs');
const fsp=require('node:fs/promises');
const path=require('node:path');
const assert=require('node:assert/strict');
const {execFileSync,spawn}=require('node:child_process');
const root=path.resolve(__dirname,'..');
const version=require('./package.json').version;
const area=fs.mkdtempSync(path.join(root,'work/Portable app 研究 '));
const zip=path.join(root,`dist/Food-Record-${version}-macOS-arm64.zip`);
const unpack=path.join(area,'Different installation');
const hidden=[];
function run(command,args,env=process.env){return new Promise((resolve,reject)=>{const p=spawn(command,args,{cwd:root,env,stdio:'inherit'});p.on('error',reject);p.on('exit',code=>code===0?resolve():reject(Error(`${path.basename(command)} exited ${code}`)));});}
(async()=>{
 execFileSync(path.join(root,'.venv/bin/python'),[path.join(root,'scripts/desktop/prepare_qa.py')],{cwd:root,stdio:'ignore'});
 execFileSync('/usr/bin/ditto',['-x','-k',zip,unpack]);
 const bundle=path.join(unpack,'Food Record.app');
 execFileSync(path.join(root,'.venv/bin/python'),[path.join(root,'scripts/desktop/normalize_bundle_symlinks.py'),bundle,'--check-only'],{stdio:'inherit'});
 const executablePath=path.join(bundle,'Contents/MacOS/Food Record');
 let app;
 try{
  for(const name of ['dist/food-record-backend','dist/macos']){
   const original=path.join(root,name),backup=original+'.unavailable-'+Date.now();
   await fsp.rename(original,backup);hidden.push({original,backup});
  }
  assert(hidden.every(p=>!fs.existsSync(p.original)));
  console.log('Original backend and packaged app paths are unavailable; running relocated ZIP');
  app=await electron.launch({executablePath,args:['--user-data-dir='+path.join(area,'new-user')],env:{...process.env,PATH:'/usr/bin:/bin'}});
  const page=await app.firstWindow();
  await page.getByRole('button',{name:'Create study',exact:true}).waitFor({timeout:60000});
  const study=path.join(area,'New researcher study');
  await app.evaluate(({dialog},filePath)=>{dialog.showSaveDialog=async()=>({canceled:false,filePath});},study);
  await page.locator('#study-name').fill('New researcher study');
  await page.getByRole('button',{name:'Create study',exact:true}).click();
  await page.getByRole('heading',{name:'New researcher study',exact:true}).waitFor({timeout:60000});
  const state=await page.evaluate(()=>fetch('/desktop/state').then(r=>r.json()));
  assert.equal(state.records.length,0);assert.equal(state.queue.active,0);assert.equal(state.queue.queued,0);
  assert(fs.existsSync(path.join(study,'workspace.json')));assert(fs.existsSync(path.join(study,'records.db')));
  await app.close();app=null;
  console.log('Relocated app created a new study without any developer workspace or API key');
  await run(process.execPath,[path.join(root,'desktop/workflow-test.cjs')],{...process.env,FOOD_RECORD_TEST_APP:executablePath});
  assert(hidden.every(p=>!fs.existsSync(p.original)));
  await fsp.writeFile(path.join(root,'work/portability-verification.json'),JSON.stringify({version,zip,relocated_bundle:bundle,original_paths_unavailable:hidden.map(p=>p.original),create_study:true,pdf_rendering:true,owner_review:true,offline_review_submit_reopen_export:true,no_ai_requests:true,passed:true},null,2));
  console.log('Portable ZIP workflow passed with both original build paths absent');
 }finally{
  if(app)await app.close();
  for(const p of hidden.reverse())await fsp.rename(p.backup,p.original);
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
