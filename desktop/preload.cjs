'use strict';
const {contextBridge, ipcRenderer, webUtils} = require('electron');
contextBridge.exposeInMainWorld('desktop', Object.freeze({
  recentStudies: () => ipcRenderer.invoke('recent-studies'),
  openRecent: index => ipcRenderer.invoke('open-recent', Number(index)),
  createStudy: name => ipcRenderer.invoke('create-study', typeof name==='string'?name:''),
  chooseInputFolder: () => ipcRenderer.invoke('choose-inputs','folder'),
  chooseInputFiles: () => ipcRenderer.invoke('choose-inputs','files'),
  takeImport: () => ipcRenderer.invoke('take-import'),
  openStudy: () => ipcRenderer.invoke('open-study'),
  openReview: () => ipcRenderer.invoke('open-review'),
  home: () => ipcRenderer.invoke('home'),
  saveKey: key => ipcRenderer.invoke('save-key', String(key)),
  deleteKey: () => ipcRenderer.invoke('delete-key'),
  restore: () => ipcRenderer.invoke('restore'),
  version: () => ipcRenderer.invoke('version'),
  // No arbitrary IPC, file paths, shell commands, or key retrieval exposed.
}));

function dropStatus(message){
  const show=()=>{let box=document.getElementById('desktop-drop-status');if(!box){box=document.createElement('div');box.id='desktop-drop-status';box.setAttribute('role','alert');document.body.append(box);}box.replaceChildren(document.createTextNode(message));const close=document.createElement('button');close.textContent='Dismiss';close.onclick=()=>box.remove();box.append(close);};
  if(document.body)show();else window.addEventListener('DOMContentLoaded',show,{once:true});
}
ipcRenderer.on('drop-status',(_event,value)=>dropStatus(String(value.message||'')));
window.addEventListener('dragover',event=>{if(event.dataTransfer?.types.includes('Files')){event.preventDefault();document.documentElement.classList.add('desktop-dragging');}},true);
window.addEventListener('dragleave',event=>{if(!event.relatedTarget)document.documentElement.classList.remove('desktop-dragging');},true);
window.addEventListener('drop',event=>{
  const files=Array.from(event.dataTransfer?.files||[]);if(!files.length)return;
  event.preventDefault();event.stopImmediatePropagation();document.documentElement.classList.remove('desktop-dragging');
  const attachmentForm=event.target.closest?.('#attachment-form');
  if(attachmentForm){
    const error=attachmentForm.querySelector('[role="alert"]');
    if(files.length!==1||!(/\.(pdf|png|jpe?g)$/i).test(files[0].name)){
      if(error)error.textContent='Drop one PDF, PNG or JPEG attachment.';
      return;
    }
    const input=attachmentForm.querySelector('input[type="file"]');
    input.files=event.dataTransfer.files;
    input.dispatchEvent(new Event('change',{bubbles:true}));
    if(error)error.textContent='';
    return;
  }
  const paths=files.map(file=>webUtils.getPathForFile(file));
  if(paths.some(p=>!p)){dropStatus('Drop files or folders directly from Finder.');return;}
  ipcRenderer.invoke('open-dropped',paths).then(result=>{if(result?.error)dropStatus(result.error);}).catch(error=>dropStatus(error.message));
},true);
