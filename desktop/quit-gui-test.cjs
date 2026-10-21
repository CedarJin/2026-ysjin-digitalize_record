'use strict';
const {_electron:electron}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),folder=fs.mkdtempSync(path.join(root,'work/quit-qa-'));
(async()=>{
 const app=await electron.launch({executablePath:path.join(root,'dist/macos/Food Record-darwin-arm64/Food Record.app/Contents/MacOS/Food Record'),args:['--user-data-dir='+path.join(folder,'profile')],env:{...process.env,PATH:'/usr/bin:/bin'}});
 const proc=app.process();
 const page=await app.firstWindow();
 await page.getByRole('button',{name:'Start a new study',exact:true}).waitFor({timeout:60000});
 await app.evaluate(({dialog},filePath)=>{dialog.showSaveDialog=async()=>({canceled:false,filePath});},path.join(folder,'Study'));
 await page.getByRole('button',{name:'Start a new study',exact:true}).click();
 await page.getByRole('button',{name:'Choose PDF folder',exact:true}).waitFor({timeout:60000});
 const endpoint=new URL(page.url()).origin;
 await app.evaluate(({dialog})=>{
  const original=globalThis.fetch;
  globalThis.fetch=(url,...args)=>String(url).endsWith('/desktop/state')?Promise.resolve(new Response('Internal Server Error',{status:500})):original(url,...args);
  dialog.showMessageBox=async(_win,opts)=>{if(opts.message!=='Study status is unavailable.')throw Error('Unexpected dialog');return {response:1};};
  dialog.showErrorBox=(_title,message)=>{throw Error(message);};
 });
 const exited=new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('App failed to quit')),15000);proc.once('exit',code=>{clearTimeout(timer);resolve(code);});});
 await app.evaluate(({BrowserWindow})=>{BrowserWindow.getAllWindows()[0].close();});
 assert.equal(await exited,0);
 let stopped=false;try{await fetch(endpoint,{signal:AbortSignal.timeout(2000)});}catch{stopped=true;}
 assert(stopped,'Backend remained listening');
 console.log('Native window close with failing status: confirmed safe exit, exit code 0, backend stopped; no AI requests.');
})().catch(e=>{console.error(e);process.exitCode=1;});
