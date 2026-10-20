'use strict';
const {packager}=require('@electron/packager');
const path=require('node:path');
const fs=require('node:fs/promises');
packager({
  dir:__dirname, out:path.resolve(__dirname,'../dist/macos'),
  platform:'darwin', arch:'arm64', name:'Food Record',
  appBundleId:'org.foodrecord.desktop', appVersion:'0.5.0',
  extendInfo:{LSMinimumSystemVersion:'13.0'},
  ignore:[/\/(smoke|workflow-test|real-reuse-test|keychain-test|lifecycle-test|package)\.cjs$/],
  overwrite:true, asar:true, prune:true,
  extraResource:[path.resolve(__dirname,'../dist/food-record-backend'),path.resolve(__dirname,'THIRD_PARTY_NOTICES.txt')],
  afterExtract:[async({buildPath})=>{
    for(const name of ['LICENSE','LICENSES.chromium.html'])
      await fs.copyFile(path.join(buildPath,name),path.join(buildPath,'Electron.app/Contents/Resources',name));
  }],
}).then(console.log).catch(e=>{console.error(e);process.exit(1);});
