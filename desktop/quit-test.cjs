'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync(require('node:path').join(__dirname,'main.cjs'),'utf8');
const quit=source.slice(source.indexOf('let quitPending=false;'),source.indexOf("app.on('before-quit'"));
function setup({failed=false,active=0,response=1,saved=true}={}){
 const calls=[];let release;
 const ctx=vm.createContext({quitting:false,switching:false,mode:'study',child:{},win:{setTitle:t=>calls.push('title')},prepareLeave:async()=>saved,local:async()=>{calls.push('status');if(failed)throw Error('Internal Server Error');return {queue:{active}};},dialog:{showMessageBox:async()=>{calls.push('confirm');return {response};},showErrorBox:()=>calls.push('error')},stop:()=>new Promise(r=>{calls.push('stop');release=r;}),app:{quit:()=>calls.push('quit')}});
 vm.runInContext(quit,ctx);return {ctx,calls,run:()=>ctx.requestQuit(),release:()=>release()};
}
test('failed status still drains backend before exiting; repeated quit does not bypass drain',async()=>{
 const s=setup({failed:true});const closing=s.run();await new Promise(setImmediate);assert.deepEqual(s.calls,['status','confirm','title','stop']);await s.run();assert(!s.calls.includes('quit'));s.release();await closing;assert.equal(s.calls.at(-1),'quit');
});
test('cancel keeps app and backend running',async()=>{const s=setup({failed:true,response:0});await s.run();assert.deepEqual(s.calls,['status','confirm']);});
test('active requests use backend drain, independent of HTTP pause',async()=>{const s=setup({active:1});const closing=s.run();await new Promise(setImmediate);assert(s.calls.includes('stop'));s.release();await closing;assert.equal(s.calls.at(-1),'quit');});
test('unsaved edits prevent exit',async()=>{const s=setup({saved:false});await s.run();assert.deepEqual(s.calls,[]);});
