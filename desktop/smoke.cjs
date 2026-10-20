const {_electron:electron}=require('playwright');
const path=require('node:path');
(async()=>{
 const app=await electron.launch({executablePath:path.resolve(__dirname,'../dist/macos/Food Record-darwin-arm64/Food Record.app/Contents/MacOS/Food Record'),args:['--user-data-dir='+path.resolve(__dirname,'../work/desktop-ui-profile')],env:{...process.env,PATH:'/usr/bin:/bin'}});
 try{
  const page=await app.firstWindow();
  await page.getByRole('button',{name:'Create study',exact:true}).waitFor({timeout:60000});
  await page.screenshot({path:path.resolve(__dirname,'../work/desktop-home.png')});
  console.log('Home loaded in packaged arm64 app');
  await app.evaluate(({dialog})=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[process.cwd()+'/work/desktop-smoke']});});
  await page.getByRole('button',{name:'Open study',exact:true}).click();
  await page.getByRole('heading',{name:'Desktop smoke study',exact:true}).waitFor({timeout:60000});
  await page.screenshot({path:path.resolve(__dirname,'../work/desktop-study.png')});
  console.log('Study opened; no Python/Node/Poppler on PATH');
  const state=await page.evaluate(()=>fetch('/desktop/state').then(r=>r.json()));
  if(state.queue.active||state.queue.queued)throw Error('Unexpected processing');
  await page.getByRole('link',{name:'Review & collaboration',exact:true}).click();
  await page.getByRole('heading',{name:'Day review queue',exact:true}).waitFor();
  console.log('Password-free local review loaded');
 }finally{await app.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
