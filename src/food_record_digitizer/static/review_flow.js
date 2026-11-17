'use strict';
// Pure queue policy shared by owner and offline-reviewer navigation.
(function(root){
  function taskFor(row, user){
    const tasks=(row.tasks||[]).filter(t=>!t.assignee_id||t.assignee_id===user.id);
    return tasks.find(t=>['assigned','in_progress'].includes(t.status))||tasks.find(t=>t.status==='submitted')||tasks[0];
  }
  function ready(row,user){
    if(!row||['excluded','retired','missing_source','blocked','exclusion_pending','approved'].includes(row.state))return false;
    const task=taskFor(row,user);
    if(task)return ['assigned','in_progress'].includes(task.status);
    return user.role==='owner'&&row.has_candidate&&row.submitted===0;
  }
  function completed(row,user){return taskFor(row,user)?.status==='submitted';}
  function next(order,rows,current,user){
    const live=new Map(rows.map(r=>[r.record_id,r]));
    const index=order.findIndex(r=>r.record_id===current);
    const sequence=[...order.slice(index+1),...order.slice(0,Math.max(index,0))];
    for(const saved of sequence){
      const row=live.get(saved.record_id);
      if(saved.input_version!==undefined&&row?.input_version!==saved.input_version)continue;
      if(ready(row,user))return row.record_id;
    }
    return null;
  }
  function issueGroups(content,blockingCodes=[]){
    const foods=content.items||[],events=content.events||[];
    const fieldNames={raw_or_cooked:'Raw or cooked',measurement_method:'Measurement method',preparation:'Preparation',food_name:'Food',quantity:'Amount',day_id:'Date',meal_label:'Meal label'};
    function locate(target){
      const match=String(target||'').match(/(?:record\.)?items\[(\d+)\](?:\.(\w+))?/);
      if(match&&foods[Number(match[1])])return {items:[Number(match[1])],field:match[2]||''};
      const indices=[];let field='';
      foods.forEach((food,i)=>{
        const entity=[food,...(food.ingredients||[])].find(x=>target===x.item_id||String(target).startsWith(x.item_id+'.'));
        const event=events.find(e=>e.event_id===food.event_id);
        if(entity){indices.push(i);field=String(target).slice(entity.item_id.length+1);}
        else if(event&&(target===event.event_id||String(target).startsWith(event.event_id+'.')||target===event.day_id)){indices.push(i);field=target===event.day_id?'Date':String(target).slice(event.event_id.length+1);}
      });
      return {items:indices,field};
    }
    const groups=new Map();
    (content.issues||[]).forEach((issue,index)=>{
      const loc=locate(issue.target),field=fieldNames[loc.field]||loc.field.replaceAll('_',' ');
      const label=loc.items.length?loc.items.map(i=>foods[i].food_name?.normalized||foods[i].description?.normalized||'Food '+(i+1)).join(', '):'This day';
      const blocking=blockingCodes.includes(issue.code);
      const message=String(issue.message||'').replace(/(?:record\.)?items\[\d+\](?:\.\w+)*/g,label);
      const key=JSON.stringify([issue.code,message,field,blocking]);
      let group=groups.get(key);
      if(!group){group={title:field||String(issue.code).replaceAll('_',' ').toLowerCase(),message,blocking,items:[],issues:[]};groups.set(key,group);}
      group.items=[...new Set([...group.items,...loc.items])];group.issues.push(index);
    });
    return [...groups.values()].sort((a,b)=>Number(b.blocking)-Number(a.blocking));
  }
  const api={taskFor,ready,completed,next,issueGroups};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  else root.ReviewFlow=api;
})(globalThis);
