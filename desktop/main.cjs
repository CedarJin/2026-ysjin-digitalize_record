'use strict';
const {app, BrowserWindow, ipcMain, dialog, safeStorage, Menu, session} = require('electron');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const readline = require('node:readline');
let win, child, origin, token, mode='home', quitting=false, switching=false, dropBusy=false, pendingImport=null, studyPath=null;
app.setName('Food Record');
if (!app.requestSingleInstanceLock()) app.quit();
app.on('second-instance', () => {win?.show(); win?.focus();});
const recentsPath=()=>path.join(app.getPath('userData'),'recent-studies.json');
function recents(){try{return JSON.parse(fs.readFileSync(recentsPath(),'utf8'));}catch{return [];}}
const keyPath=()=>path.join(app.getPath('userData'),'api-key.enc');
function readKey(){if(!fs.existsSync(keyPath()))return null;if(!safeStorage.isEncryptionAvailable())throw Error('Keychain is unavailable. Unlock your Mac and try again.');return safeStorage.decryptString(fs.readFileSync(keyPath()));}
async function local(route, body){if(!origin)throw Error('Study is not ready.');const r=await fetch(origin+route,{method:body===undefined?'GET':'POST',headers:{'X-Desktop-Capability':token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});if(!r.ok)throw Error((await r.json()).error||'Operation failed.');return r.json();}
async function stop(){if(!child)return;const old=child;child=null;await new Promise(resolve=>{if(old.exitCode!==null)return resolve();old.once('exit',resolve);old.stdin.end(JSON.stringify({action:'stop'})+'\n');});}
async function prepareLeave(requireSaved=false){if(!win||!origin)return true;return win.webContents.executeJavaScript(`typeof window.prepareDesktopNavigation === 'function' ? window.prepareDesktopNavigation(${requireSaved}) : true`);}
function rememberStudy(file,state){
  const list=[{path:file,name:state.name,opened:new Date().toISOString(),records:state.records.length},...recents().filter(x=>x.path!==file)].slice(0,6);
  fs.mkdirSync(app.getPath('userData'),{recursive:true});fs.writeFileSync(recentsPath(),JSON.stringify(list),{mode:0o600});
}
async function canSwitch(){if(!await prepareLeave(true))return false;if(mode!=='study'||!child)return true;const state=await local('/desktop/state');if(state.queue.active){await dialog.showMessageBox(win,{type:'info',message:'Requests are still processing.',detail:'Pause the queue and wait for current requests before switching studies.',buttons:['Keep processing']});return false;}if(studyPath)rememberStudy(studyPath,state);return true;}
async function start(config={mode:'home'}){
  if(switching)return;switching=true;
  try{
    await stop(); token=crypto.randomBytes(32).toString('hex');origin=null;mode=config.mode;
    const backend=app.isPackaged?path.join(process.resourcesPath,'food-record-backend','food-record-backend'):path.resolve(__dirname,'../dist/food-record-backend/food-record-backend');
    if(!fs.existsSync(backend))throw Error('Bundled backend is missing. Build the desktop backend first.');
    const env={PYINSTALLER_RESET_ENVIRONMENT:'1',PATH:'/usr/bin:/bin',HOME:app.getPath('home'),TMPDIR:app.getPath('temp'),LANG:'en_US.UTF-8'};
    child=spawn(backend,[],{stdio:['pipe','pipe','pipe'],env});
    let startupError='';child.stderr.on('data',d=>{startupError+=d.toString().slice(0,2000);});
    const launched=child;
    const ready=new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(Error('The study could not start within 60 seconds.')),60000);
      readline.createInterface({input:launched.stdout}).once('line',line=>{clearTimeout(timer);try{resolve(JSON.parse(line));}catch{reject(Error('Backend startup failed.'));}});
      launched.once('error',e=>{clearTimeout(timer);reject(e);});
      launched.once('exit',()=>{clearTimeout(timer);reject(Error(startupError.includes('already open')?'This study is already open.':startupError.includes('newer desktop')?'This study requires a newer app.':'Unable to open this study. Check its location, format and write permissions.'));});
    });
    launched.stdin.write(JSON.stringify({...config,capability:token,review_root:path.join(app.getPath('userData'),'reviews'),key:config.mode==='study'?readKey():null})+'\n');
    const result=await ready;origin='http://127.0.0.1:'+result.port;
    // Wait until the socket is accepting HTTP before navigating.
    for(let n=0;n<100;n++){try{await fetch(origin+'/',{headers:{'X-Desktop-Capability':token}});break;}catch{await new Promise(r=>setTimeout(r,50));}}
    launched.on('exit',()=>{if(child===launched&&!quitting&&!switching){child=null;dialog.showErrorBox('Study closed unexpectedly','Your saved work is retained. Reopen the app; interrupted requests will not restart automatically.');}});
    if(config.mode==='study'){const info=await local('/desktop/state');studyPath=info.path;rememberStudy(studyPath,info);}
    await win.loadURL(origin+(config.mode==='review'?'/?launch='+encodeURIComponent(token):config.create?'/?view=import':'/'));
  }finally{switching=false;}
}
async function restoreFrom(source){
  if(!await canSwitch())return;
  const dest=await dialog.showSaveDialog(win,{title:'Restore into a new study folder',defaultPath:path.join(app.getPath('documents'),'Restored study'),buttonLabel:'Restore'});
  if(dest.canceled)return;if(fs.existsSync(dest.filePath))throw Error('Choose a new folder. Existing studies will not be overwritten.');
  await start({mode:'study',path:dest.filePath,restore_from:source});
}
async function openArchive(file){
  const inspected=await local('/desktop/inspect-archive',{path:file});
  if(inspected.kind==='task'){if(await canSwitch())await start({mode:'review',path:file});return;}
  if(inspected.kind==='backup'){await restoreFrom(file);return;}
  if(mode!=='study')throw Error('This ZIP contains returned results. Continue the study that issued it, then use Study tools → Review packages → Import returned results.');
  if(!await prepareLeave(true))return;
  await win.loadURL(origin+'/review#offline');
  win.webContents.send('drop-status',{message:'Returned results detected. Select this ZIP under Import returned results: '+path.basename(file)});
}
async function importPaths(paths){
  if(!await prepareLeave(true))return;
  const pdfs=[],metadata=[];let ignored=0,visited=0,totalBytes=0;
  async function visit(file,relative){
    if(++visited>10000)throw Error('This folder contains too many files. Choose a smaller records folder.');
    if(relative.split('/').length>32)throw Error('This folder is nested too deeply. Choose a smaller records folder.');
    const info=await fs.promises.lstat(file);if(info.isSymbolicLink()){ignored++;return;}
    if(info.isDirectory()){
      if(fs.existsSync(path.join(file,'workspace.json'))&&(fs.existsSync(path.join(file,'records.db'))||fs.existsSync(path.join(file,'.app','records.db'))))throw Error('This is an existing study folder. Use Continue a study to open it.');
      for(const entry of (await fs.promises.readdir(file,{withFileTypes:true})).sort((a,b)=>a.name.localeCompare(b.name))){
        if(entry.name.startsWith('.'))continue;
        await visit(path.join(file,entry.name),relative?relative+'/'+entry.name:entry.name);
      }
      return;
    }
    if(!info.isFile())return;
    const suffix=path.extname(file).toLowerCase();
    if(suffix==='.pdf'){if(info.size>32*1024*1024)throw Error('Each PDF must be at most 32 MB.');totalBytes+=info.size;if(totalBytes>512*1024*1024)throw Error('Choose up to 512 MB of PDFs per import. Split larger folders into batches.');pdfs.push({file,name:relative||path.basename(file)});}
    else if(['.csv','.xlsx','.json'].includes(suffix)){if(info.size>4*1024*1024)throw Error('Each metadata file must be at most 4 MB.');metadata.push(file);}
    else ignored++;
    if(pdfs.length>1000)throw Error('Import up to 1000 PDFs at a time.');
  }
  for(const file of paths)await visit(file,fs.statSync(file).isDirectory()?'':path.basename(file));
  if(!pdfs.length)throw Error('No supported PDF scans were found. Choose a folder containing PDFs.');
  let meta=metadata[0];
  if(metadata.length>1){
    const chosen=await dialog.showOpenDialog(win,{title:'Several metadata files found — choose the one for this import',defaultPath:path.dirname(metadata[0]),properties:['openFile'],filters:[{name:'Metadata',extensions:['csv','xlsx','json']}]});
    if(chosen.canceled)return;meta=chosen.filePaths[0];
    if(fs.statSync(meta).size>4*1024*1024)throw Error('Metadata must be at most 4 MB.');
  }
  const form=new FormData();
  for(const file of pdfs)form.append('files',new Blob([await fs.promises.readFile(file.file)],{type:'application/pdf'}),file.name);
  if(meta)form.append('metadata',new Blob([await fs.promises.readFile(meta)]),path.basename(meta));
  const response=await fetch(origin+'/desktop/import/upload',{method:'POST',headers:{'X-Desktop-Capability':token},body:form});
  const data=await response.json();if(!response.ok)throw Error(data.error||'The folder could not be prepared for import.');
  pendingImport={...data,ignored_files:ignored};
  await win.loadURL(origin+'/?view=import-ready');
}
async function openDropped(paths){
  if(dropBusy||switching)throw Error('A file is already being opened. Please wait.');
  dropBusy=true;
  try{
    if(!Array.isArray(paths)||!paths.length||paths.length>1000||paths.some(p=>typeof p!=='string'||!p||!path.isAbsolute(p)))throw Error('Drop a local file or folder from Finder.');
    if(paths.length===1&&path.extname(paths[0]).toLowerCase()==='.zip')return await openArchive(paths[0]);
    if(paths.some(p=>path.extname(p).toLowerCase()==='.zip'))throw Error('Open one review ZIP at a time.');
    if(mode!=='study')throw Error('Start a new study or continue a study before adding record files.');
    return await importPaths(paths);
  }finally{dropBusy=false;}
}
function handle(name, fn){ipcMain.handle(name,async(event,...args)=>{if(event.sender!==win.webContents||event.senderFrame!==win.webContents.mainFrame||new URL(event.senderFrame.url).origin!==origin)throw Error('Untrusted window.');try{return await fn(...args);}catch(e){if(!origin){try{await start();}catch{}}if(name!=='open-dropped')await dialog.showMessageBox(win,{type:'error',message:e.message,buttons:['OK']});return {error:e.message};}});}
app.whenReady().then(async()=>{
  win=new BrowserWindow({width:1320,height:880,minWidth:960,minHeight:640,show:false,title:'Food Record',webPreferences:{preload:path.join(__dirname,'preload.cjs'),nodeIntegration:false,contextIsolation:true,sandbox:true,webSecurity:true}});
  session.defaultSession.webRequest.onBeforeRequest((details,callback)=>{const url=details.url;callback({cancel:!(origin&&url.startsWith(origin+'/'))&&!url.startsWith('devtools:')&&!url.startsWith('blob:')});});
  session.defaultSession.webRequest.onBeforeSendHeaders((details,callback)=>{if(origin&&details.url.startsWith(origin+'/'))details.requestHeaders['X-Desktop-Capability']=token;callback({requestHeaders:details.requestHeaders});});
  session.defaultSession.setPermissionRequestHandler((_w,_p,cb)=>cb(false));
  session.defaultSession.on('will-download',(_event,item)=>{let folder=app.getPath('downloads');if(mode==='study'&&studyPath){const organized=fs.existsSync(path.join(studyPath,'.app','workspace.json'));folder=path.join(studyPath,organized?'results/exports':'exports');fs.mkdirSync(folder,{recursive:true});}item.setSaveDialogOptions({title:'Save research file',defaultPath:path.join(folder,path.basename(item.getFilename()))});});
  win.webContents.setWindowOpenHandler(()=>({action:'deny'}));
  win.webContents.on('will-navigate',(event,url)=>{if(!origin||new URL(url).origin!==origin)event.preventDefault();});
  win.on('close',event=>{if(!quitting){event.preventDefault();requestQuit();}});
  handle('recent-studies',()=>recents());
  handle('open-recent',async index=>{const saved=recents();if(!Number.isInteger(index)||!saved[index])throw Error('Choose a recent study from the list.');if(await canSwitch())await start({mode:'study',path:saved[index].path});});
  handle('version',()=>({version:app.getVersion(),arch:process.arch}));
  handle('create-study',async name=>{name=typeof name==='string'?name.trim():'';if(name.length>200)throw Error('Use a study name of 200 characters or fewer.');if(!await canSwitch())return;const result=await dialog.showSaveDialog(win,{title:'Create study folder',defaultPath:path.join(app.getPath('documents'),name.replace(/[^\p{L}\p{N} _-]/gu,'').slice(0,80)||'New study'),buttonLabel:'Create study'});if(result.canceled)return;if(fs.existsSync(result.filePath))throw Error('Choose a new folder name to avoid replacing existing files.');await start({mode:'study',path:result.filePath,create:true,name:name||path.basename(result.filePath)});});
  handle('open-study',async()=>{if(!await canSwitch())return;const result=await dialog.showOpenDialog(win,{title:'Open study folder',properties:['openDirectory']});if(!result.canceled)await start({mode:'study',path:result.filePaths[0]});});
  handle('open-review',async()=>{if(!await canSwitch())return;const result=await dialog.showOpenDialog(win,{title:'Open review package',properties:['openFile'],filters:[{name:'Review package',extensions:['zip']}]});if(!result.canceled)await openArchive(result.filePaths[0]);});
  handle('restore',async()=>{const source=await dialog.showOpenDialog(win,{title:'Choose study backup',properties:['openFile'],filters:[{name:'Study backup',extensions:['zip']}]});if(!source.canceled)await openArchive(source.filePaths[0]);});
  handle('open-dropped',openDropped);
  handle('take-import',()=>{const data=pendingImport;pendingImport=null;return data;});
  handle('choose-inputs',async kind=>{if(mode!=='study')throw Error('Create or continue a study first.');if(!['folder','files'].includes(kind))throw Error('Choose files or a folder.');const chosen=await dialog.showOpenDialog(win,{title:kind==='folder'?'Choose folder with records and metadata':'Choose PDF records and metadata',properties:kind==='folder'?['openDirectory']:['openFile','multiSelections']});if(!chosen.canceled)return openDropped(chosen.filePaths);});
  handle('organized-copy',async()=>{if(mode!=='study')throw Error('Open a study first.');if(!await canSwitch())return;const chosen=await dialog.showSaveDialog(win,{title:'Save organized study copy',defaultPath:studyPath+' (organized)',buttonLabel:'Save copy'});if(chosen.canceled)return;await local('/desktop/organized-copy',{path:chosen.filePath});await start({mode:'study',path:chosen.filePath});});
  handle('home',async()=>{if(await canSwitch())await start();});
  handle('save-key',key=>{if(mode!=='study')throw Error('Open a study first.');if(!key.trim()||key.length>1024)throw Error('Enter a valid API key.');if(!safeStorage.isEncryptionAvailable())throw Error('Keychain is unavailable.');fs.mkdirSync(app.getPath('userData'),{recursive:true});fs.writeFileSync(keyPath(),safeStorage.encryptString(key.trim()),{mode:0o600});child.stdin.write(JSON.stringify({action:'key',key:key.trim()})+'\n');return {saved:true};});
  handle('delete-key',()=>{if(mode!=='study')throw Error('Open a study first.');if(fs.existsSync(keyPath()))fs.unlinkSync(keyPath());child?.stdin.write(JSON.stringify({action:'key',key:null})+'\n');return {deleted:true};});
  Menu.setApplicationMenu(Menu.buildFromTemplate([{label:'Food Record',submenu:[{role:'about'},{label:'Open study…',click:()=>win.webContents.executeJavaScript('window.desktop.openStudy()')},{label:'Open review package…',click:()=>win.webContents.executeJavaScript('window.desktop.openReview()')},{type:'separator'},{label:'Quit Food Record',accelerator:'Cmd+Q',click:requestQuit}]},{role:'editMenu'},{role:'viewMenu'}]));
  try{await start();win.show();}catch(e){dialog.showErrorBox('Unable to start Food Record',e.message);quitting=true;app.quit();}
});
async function requestQuit(){if(quitting||switching)return;try{if(!await prepareLeave())return;if(mode==='study'&&child){const state=await local('/desktop/state');if(state.queue.active){const choice=await dialog.showMessageBox(win,{type:'question',message:'Processing is still running.',detail:'Stop after current requests pauses new work, saves active results, then closes. Already sent requests may incur charges.',buttons:['Keep processing','Stop after current requests'],defaultId:0,cancelId:0});if(choice.response===0)return;await local('/desktop/process/pause',{});win.setTitle('Food Record — saving current results before closing');}}quitting=true;await stop();app.quit();}catch(e){dialog.showErrorBox('Unable to close safely',e.message);}}
app.on('before-quit',event=>{if(!quitting){event.preventDefault();requestQuit();}});
