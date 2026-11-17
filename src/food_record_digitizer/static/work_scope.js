/* A source selection expands to whole review days. A day selection stays exact,
   even when one PDF contains several days. Storage is isolated by study path. */
(function(root){
  const key=study=>'food-record-scope:'+study;
  const read=(storage,study)=>{try{const x=JSON.parse(storage.getItem(key(study))||'null');return x&&Array.isArray(x.sources)&&(!x.days||Array.isArray(x.days))?x:{sources:[],days:null};}catch{return {sources:[],days:null};}};
  const save=(storage,study,value)=>{try{storage.setItem(key(study),JSON.stringify(value));}catch{}return value;};
  const sources=(storage,study,ids)=>save(storage,study,{sources:[...new Set(ids)],days:null});
  const days=(storage,study,ids,rows)=>{const chosen=new Set(ids),matched=rows.filter(r=>chosen.has(r.record_id));return save(storage,study,{days:matched.map(r=>r.record_id),sources:[...new Set(matched.flatMap(r=>r.source_record_ids||[]))]});};
  const resolve=(scope,rows)=>rows.filter(r=>scope.days?scope.days.includes(r.record_id):(r.source_record_ids||[]).some(id=>scope.sources.includes(id)));
  const api={read,sources,days,resolve};if(typeof module!=='undefined')module.exports=api;else root.WorkScope=api;
})(globalThis);
