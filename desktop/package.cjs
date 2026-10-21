'use strict';
const {packager}=require('@electron/packager');
const path=require('node:path');
const fs=require('node:fs/promises');
const {execFileSync}=require('node:child_process');
const root=path.resolve(__dirname,'..');
const version=require('./package.json').version;
const validate=bundle=>execFileSync(path.join(root,'.venv/bin/python'),[
  path.join(root,'scripts/desktop/normalize_bundle_symlinks.py'),bundle,'--check-only'
],{stdio:'inherit'});
packager({
  dir:__dirname, out:path.resolve(__dirname,'../dist/macos'),
  platform:'darwin', arch:'arm64', name:'Food Record',
  appBundleId:'org.foodrecord.desktop', appVersion:version,
  extendInfo:{LSMinimumSystemVersion:'13.0'},
  ignore:[/\/(smoke|package|.*-test)\.cjs$/],
  overwrite:true, asar:true, prune:true,
  extraResource:[path.resolve(__dirname,'THIRD_PARTY_NOTICES.txt')],
  afterCopyExtraResources:[async({buildPath})=>{
    const source=path.join(root,'dist/food-record-backend');
    validate(source);
    // Node's default fs.cp rewrites relative symlinks to absolute source paths.
    await fs.cp(source,path.join(buildPath,'Food Record.app/Contents/Resources/food-record-backend'),{
      recursive:true,verbatimSymlinks:true,
    });
  }],
  afterExtract:[async({buildPath})=>{
    for(const name of ['LICENSE','LICENSES.chromium.html'])
      await fs.copyFile(path.join(buildPath,name),path.join(buildPath,'Electron.app/Contents/Resources',name));
  }],
}).then(paths=>{
  for(const output of paths)validate(path.join(output,'Food Record.app'));
  console.log(paths);
}).catch(e=>{console.error(e);process.exit(1);});
