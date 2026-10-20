'use strict';
const {contextBridge, ipcRenderer} = require('electron');
contextBridge.exposeInMainWorld('desktop', Object.freeze({
  recentStudies: () => ipcRenderer.invoke('recent-studies'),
  openRecent: index => ipcRenderer.invoke('open-recent', Number(index)),
  createStudy: name => ipcRenderer.invoke('create-study', String(name)),
  openStudy: () => ipcRenderer.invoke('open-study'),
  openReview: () => ipcRenderer.invoke('open-review'),
  home: () => ipcRenderer.invoke('home'),
  saveKey: key => ipcRenderer.invoke('save-key', String(key)),
  deleteKey: () => ipcRenderer.invoke('delete-key'),
  restore: () => ipcRenderer.invoke('restore'),
  version: () => ipcRenderer.invoke('version'),
  // No arbitrary IPC, file paths, shell commands, or key retrieval exposed.
}));
