'use strict';
const {_electron:electron}=require('playwright');
const assert=require('node:assert/strict');
const {execFileSync}=require('node:child_process');
const fs=require('node:fs');
const path=require('node:path');
const root=path.resolve(__dirname,'..');
const study=JSON.parse(fs.readFileSync(path.join(root,'work/desktop-qa-manifest.json'))).study;
const profile=path.join(root,'work/lifecycle-qa-'+Date.now());
const delay=ms=>new Promise(r=>setTimeout(r,ms));
async function closed(url){
 for(let i=0;i<100;i++){
  try{await fetch(url,{signal:AbortSignal.timeout(500)});}catch{return;}
  await delay(50);
 }
 throw Error('Backend is still listening after closing');
}
(async()=>{
 const app=await electron.launch({executablePath:path.join(root,'dist/macos/Food Record-darwin-arm64/Food Record.app/Contents/MacOS/Food Record'),args:['--user-data-dir='+profile],env:{...process.env,PATH:'/usr/bin:/bin'}});
 const appProcess=app.process();
 try{
  const page=await app.firstWindow();
  await page.getByRole('button',{name:'Continue a study',exact:true}).waitFor({timeout:60000});
  await app.evaluate(({dialog})=>{globalThis.errors=[];dialog.showErrorBox=(title,message)=>errors.push(message);dialog.showMessageBox=async(_win,options)=>{errors.push(options.message);return {response:0};};});
  const initial=page.url();
  await app.evaluate(({dialog},folder)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[folder]});},path.join(root,'work'));
  await page.getByRole('button',{name:'Continue a study',exact:true}).click();
  for(let i=0;i<100 && page.url()===initial;i++)await delay(100);
  await page.getByRole('button',{name:'Continue a study',exact:true}).waitFor({timeout:60000});
  assert((await app.evaluate(()=>errors)).some(s=>s.includes('Unable to open')));
  await closed(initial);
  await app.evaluate(({dialog},folder)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[folder]});},study);
  await page.getByRole('button',{name:'Continue a study',exact:true}).click();
  await page.getByRole('button',{name:'Add records',exact:true}).waitFor({timeout:60000});
  const endpoint=new URL(page.url()).origin;
  const children=execFileSync('/bin/ps',['-axo','pid=,ppid=,comm='],{encoding:'utf8'}).split('\n').map(s=>s.trim().split(/\s+/));
  const backend=children.find(row=>Number(row[1])===appProcess.pid&&row.slice(2).join(' ').endsWith('food-record-backend'));
  assert(backend,'No app-owned backend found');
  process.kill(Number(backend[0]),'SIGKILL');
  await closed(endpoint);
  for(let i=0;i<100;i++){if((await app.evaluate(()=>errors)).some(s=>s.includes('saved work')))break;await delay(50);}
  assert((await app.evaluate(()=>errors)).some(s=>s.includes('saved work')));
  await app.close();
  console.log('Invalid study recovered to home; replaced backend closed; unexpected backend exit reported; app closed');
 }finally{if(appProcess.exitCode===null)await app.close();}
 const reopened=await electron.launch({executablePath:path.join(root,'dist/macos/Food Record-darwin-arm64/Food Record.app/Contents/MacOS/Food Record'),args:['--user-data-dir='+profile],env:{...process.env,PATH:'/usr/bin:/bin'}});
 const reopenedProcess=reopened.process();
 try{
  const page=await reopened.firstWindow();
  await page.getByRole('button',{name:'Continue a study',exact:true}).waitFor({timeout:60000});
  await reopened.evaluate(({dialog},folder)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[folder]});},study);
  await page.getByRole('button',{name:'Continue a study',exact:true}).click();
  await page.getByRole('button',{name:'Add records',exact:true}).waitFor({timeout:60000});
  const endpoint=new URL(page.url()).origin;
  await reopened.close();await closed(endpoint);
  console.log('Reopened same study after crash; normal quit leaves no listening backend');
 }finally{if(reopenedProcess.exitCode===null)await reopened.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
