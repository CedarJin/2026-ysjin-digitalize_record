'use strict';
const {_electron:electron}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const profile=path.resolve(__dirname,'../work/keychain-qa-'+Date.now());
const study=JSON.parse(fs.readFileSync(path.resolve(__dirname,'../work/desktop-qa-manifest.json'))).study;
const fake='synthetic-key-not-a-real-api-credential';
async function launch(){
 const app=await electron.launch({executablePath:path.resolve(__dirname,'../dist/macos/Food Record-darwin-arm64/Food Record.app/Contents/MacOS/Food Record'),args:['--user-data-dir='+profile],env:{...process.env,PATH:'/usr/bin:/bin'}});
 const page=await app.firstWindow();
 await page.getByRole('button',{name:'Open study',exact:true}).waitFor({timeout:60000});
 await app.evaluate(({dialog},study)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[study]});},study);
 await page.getByRole('button',{name:'Open study',exact:true}).click();
 await page.getByRole('button',{name:'Add records',exact:true}).waitFor({timeout:60000});
 return {app,page};
}
(async()=>{
 let current;
 try{
  current=await launch();
  const saved=await current.page.evaluate(key=>window.desktop.saveKey(key),fake);
  if(saved?.error)throw Error(saved.error);
  const encrypted=fs.readFileSync(path.join(profile,'api-key.enc'));
  if(encrypted.includes(Buffer.from(fake)))throw Error('Credential stored as plaintext');
  await current.app.close();current=null;
  current=await launch();
  const state=await current.page.evaluate(()=>fetch('/desktop/state').then(r=>r.json()));
  assert.equal(state.has_key,true);
  assert.equal(state.queue.active,0);assert.equal(state.queue.queued,0);
  await current.page.evaluate(()=>window.desktop.deleteKey());
  assert.equal(fs.existsSync(path.join(profile,'api-key.enc')),false);
  console.log('Keychain-protected synthetic key: saved encrypted, read after restart, deleted; no requests');
 }finally{if(current)await current.app.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
