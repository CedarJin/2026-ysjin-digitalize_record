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
  const api={taskFor,ready,completed,next};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  else root.ReviewFlow=api;
})(globalThis);
