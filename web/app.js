'use strict';
const $=s=>document.querySelector(s);
let data=null,token=null,page=0,plan=null,job=null,busy=false,stopped=false;
const states={0:'未知',1:'准备',2:'等待',3:'排队',4:'启动中',5:'正在下载',6:'停止中',7:'暂停',8:'完成',9:'失败',10:'做种'};
const outcomes={started:'启动已受理',paused:'已暂停',deleted:'已删除任务',skipped:'状态或身份变化，已跳过',blocked:'已有操作记录，已阻止重复操作',uncertain:'结果未确认，请到迅雷核对'};
function node(tag,text,cls){const e=document.createElement(tag);e.textContent=text;if(cls)e.className=cls;return e}
function fmt(n){if(n==null)return '未知';let u=0;while(n>=1024&&u<4){n/=1024;u++}return n.toFixed(u?1:0)+' '+['B','KiB','MiB','GiB','TiB'][u]}
function progress(t){return t.size>0?(t.received*100/t.size).toFixed(1)+'%':'未知'}
async function post(path,body,{timeoutMs=180000}={}){
 if(!token)throw Error('尚未连接服务，请刷新页面');
 const controller=new AbortController();let timer;
 try{
  return await Promise.race([
   (async()=>{
    const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Monitor-Token':token},body:JSON.stringify(body),signal:controller.signal});
    if(r.status===401){location.replace('/login');throw Error('请重新登录')}
    let v;try{v=await r.json()}catch(e){throw Error('服务请求失败，请刷新页面核对执行记录')}
    if(!r.ok)throw Error(v.error||'操作失败');return v;
   })(),
   new Promise((_,reject)=>{timer=setTimeout(()=>{controller.abort();reject(Error('请求超时。若已确认执行，请先查看执行记录核对结果，不要重复提交。'))},timeoutMs)})
  ]);
 }finally{clearTimeout(timer)}
}
function machineName(id){const h=data?.hosts.find(h=>h.id===id);return h?.name||id}
function error(e){$('#error').textContent=e.message||String(e)}
function taskView(){return $('#task-view').value||'incomplete'}
function archiveOf(host,view){return host.archives?.[view]||{tasks:[],state:'pending'}}
function archiveCount(host,view){const a=archiveOf(host,view);return a.state==='ready'&&Date.now()/1000-(a.checked_at||0)<=180?String(a.tasks.length):'—'}
function dateTime(value){return typeof value==='number'&&value>0?new Date(value<1e12?value*1000:value).toLocaleString():'未知'}
function readOnlyTask(task){return task.read_only===true||task.view!=='incomplete'||(task.recycle_time||0)>0}
function selectTaskView(view,host=''){$('#task-view').value=view;$('#host').value=host;page=0;showPage('tasks');render();syncAutomation(true)}
function render(){
 if(!data)return;
 const view=taskView(),readOnly=view!=='incomplete';
 $('#empty-workspace').hidden=data.hosts.length>0;renderDashboard();
 $('.badge').textContent=`${data.hosts.filter(h=>h.auto_enabled).length} 台自动清理 · 保留文件`;
 $('#summary').textContent=`${data.hosts.length} 台主机 · 下载每 ${data.poll_seconds} 秒采样 · 已完成与回收站每 60 秒更新`;
 $('#filter').disabled=readOnly;$('#bulk').hidden=readOnly;$('#bulk-note').hidden=readOnly;$('#automation-settings').hidden=readOnly;
 $('#time-column').textContent=view==='recycle'?'删除时间':view==='completed'?'完成时间':'连续零速';
 const selected=data.hosts.filter(h=>!$('#host').value||$('#host').value===h.id);
 $('#view-note').textContent=readOnly?(view==='recycle'?'回收站不参与普通暂停、删除或一键加速；转存成功后会清理对应回收站记录，保留本地文件。':'已完成任务仅供查看。')+' '+selected.map(h=>{const a=archiveOf(h,view);return machineName(h.id)+'：'+(a.state==='pending'?'等待首次读取':a.state==='error'?'读取失败，显示上次结果':('更新于 '+dateTime(a.checked_at))) }).join('；'):'只对正在下载的任务累计零速时间，排队、暂停与回收站均不参与自动清理。';
 const cards=$('#cards');cards.replaceChildren();let rows=[];
 for(const h of data.hosts){
  const c=node('div','','card');c.append(node('strong',machineName(h.id)),node('div',h.address,'muted'),node('div',h.state,h.state==='在线'?'good':'bad'),node('div',h.error||`${h.tasks.length} 未完成 · 已完成 ${archiveCount(h,'completed')} · 回收站 ${archiveCount(h,'recycle')}`));
  c.append(node('div',h.auto_enabled?'自动清理开启 · '+h.idle_seconds+' 秒':'自动清理关闭','muted'));c.style.cursor='pointer';
  c.onclick=()=>{$('#host').value=h.id;page=0;showPage('tasks');render();syncAutomation(true)};cards.append(c);
  if(!$('#host').value||$('#host').value===h.id){const source=readOnly?archiveOf(h,view).tasks:h.tasks;rows.push(...source.map(t=>({...t,host:h.id,view,read_only:readOnly||t.read_only===true})))}
 }
 const filter=$('#filter').value,q=$('#search').value.toLocaleLowerCase();
 rows=rows.filter(t=>(readOnly||filter==='incomplete'||filter==='active'&&t.status===5&&t.active||filter==='queued'&&t.status===3||filter==='zero'&&t.status===5&&t.active&&t.speed===0||filter==='review'&&t.needs_review)&&((t.name||'')+' '+t.id).toLocaleLowerCase().includes(q));
 rows.sort((a,b)=>readOnly?((view==='recycle'?b.recycle_time-a.recycle_time:b.completed_at-a.completed_at)||a.host.localeCompare(b.host)||a.id-b.id):(a.host.localeCompare(b.host)||a.id-b.id));
 const pages=Math.max(1,Math.ceil(rows.length/50));page=Math.max(0,Math.min(page,pages-1));$('#page').textContent=`${rows.length} 个任务 · 第 ${page+1}/${pages} 页`;$('#prev').disabled=page===0;$('#next').disabled=page>=pages-1;
 const tbody=$('#rows');tbody.replaceChildren();
 for(const t of rows.slice(page*50,(page+1)*50)){
  const tr=node('tr','');
  const state=t.view==='recycle'?'回收站 · 原状态 '+(states[t.status]||'未知'):states[t.status]||'未知';
  const time=t.view==='recycle'?dateTime(t.recycle_time):t.view==='completed'?dateTime(t.completed_at):(t.unchanged_seconds||0)+' 秒';
  [machineName(t.host)+' / '+t.id,t.name,state,progress(t)+(readOnly?'':' · '+fmt(t.speed)+'/s'),time].forEach(v=>tr.append(node('td',v)));
  const td=node('td','');const actions=readOnlyTask(t)?[['详情','show']]:[['详情','show'],['移除·保留文件','del'],['删除文件','delf']];
  if(t.view==='recycle'&&cloudEligible(t))actions.push(['转存云盘','cloud']);
  for(const [label,command] of actions){const button=node('button',label,command==='delf'?'danger':'');button.disabled=busy&&command!=='show';button.onclick=()=>command==='show'?detail(t):command==='cloud'?openCloud('recycle',t):preview(t.host,command,t.id);td.append(button)}
  tr.append(td);tbody.append(tr);
 }
 if(!rows.length){const tr=node('tr',''),td=node('td',readOnly&&selected.some(h=>archiveOf(h,view).state!=='ready')?'暂无可显示的记录，请等待读取或刷新所选机器':'暂无任务');td.colSpan=6;tr.append(td);tbody.append(tr)}
 $('#events').textContent=(data.events||[]).filter(e=>!$('#host').value||e.host===$('#host').value).slice(-8).reverse().map(e=>new Date(e.time*1000).toLocaleTimeString()+' '+e.host+' #'+e.task_id+' '+({attempt_started:'自动清理：开始复查',removed_keep_files:'自动移除，保留文件',skipped_changed:'复查变化，跳过',uncertain_no_retry:'结果未确认，不重试',manual_started:'手动操作开始',manual_paused:'手动暂停成功',manual_resumed:'手动启动成功',manual_deleted_files:'手动删除任务及文件',manual_deleted_keep_files:'手动移除，保留文件',manual_skipped_changed:'手动复查变化，跳过',manual_uncertain:'手动结果未确认'}[e.event]||e.event)).join('；')||'暂无操作记录';
 document.querySelectorAll('#bulk button').forEach(b=>b.disabled=readOnly||busy||!$('#host').value);syncAutomation(false);
}
function detail(t){
 const view=t.view==='recycle'?'回收站':t.view==='completed'?'已完成':'下载与排队';
 $('#detail-text').textContent=`${t.name}\n任务 ID：${t.id}\n机器：${machineName(t.host)}\n所属列表：${view}${readOnlyTask(t)?'（仅查看）':''}\n原任务状态：${states[t.status]||'未知'}\n进度：${progress(t)}\n已下载：${fmt(t.received)} / ${fmt(t.size)}\n保存位置：${t.save_path}\n创建时间：${dateTime(t.created_at)}\n完成时间：${dateTime(t.completed_at)}`+(t.view==='recycle'?`\n删除时间：${dateTime(t.recycle_time)}`:'')+`\n下载链接：${t.url||'未返回链接'}\n${readOnlyTask(t)?'此列表不提供普通暂停、删除或自动清理；回收站可单独转存云盘，缓存完成后清理对应记录并保留本地文件。':'自动清理：'+(t.protection_reason||'等待采样')}`;
 $('#detail-transfer').hidden=!(t.view==='recycle'&&cloudEligible(t));$('#detail-transfer').onclick=()=>{$('#detail').close();openCloud('recycle',t)};
 $('#detail').showModal();
}
async function requestPreview(body,update){
 const ticket=await post('/api/previews/start',body,{timeoutMs:15000});const started=Date.now();
 for(;;){
  const r=await post('/api/previews/status',{preview:ticket.preview},{timeoutMs:15000});
  if(r.state==='done')return r.result;
  if(r.state==='error')throw Error(r.error);
  update?.((r.state==='queued'?'预览排队中':'正在读取目标机器')+' · '+Math.floor((Date.now()-started)/1000)+' 秒');
  if(Date.now()-started>300000)throw Error('预览仍在后台处理中，请稍后重新点击预览核对；尚未执行操作。');
  await new Promise(resolve=>setTimeout(resolve,1500));
 }
}
async function preview(host,command,id){
 if(taskView()!=='incomplete'){error(Error('此列表仅供查看'));return}if(busy)return;
 busy=true;render();$('#error').textContent='正在生成预览…';
 try{
  plan=await requestPreview({kind:'manual',host,command,task_id:id,progress_limit:['pause_zero','del_zero','delf_zero'].includes(command)&&$('#manual-limit').value!==''?Number($('#manual-limit').value):null},message=>$('#error').textContent=message);
  $('#error').textContent='';$('#preview-title').textContent=plan.label;
  $('#preview-note').textContent=`机器 ${machineName(plan.host)}，匹配 ${plan.count} 个任务。请在 ${Math.round(plan.expires_seconds/60)} 分钟内确认；已受理的任务不受此时效限制，执行时逐条核对。`+(plan.files?'本地文件也将被删除。':'本地文件保留。');
  const list=$('#preview-tasks');list.replaceChildren(...plan.tasks.map(t=>node('p',`#${t.id} ${t.name}\n保存位置：${t.path}`)));
  $('#confirm-label').hidden=!plan.files;$('#confirm-text').value='';$('#preview-error').textContent='';$('#execute').textContent='确认执行';$('#execute').disabled=plan.count===0;$('#preview').showModal();
 }catch(e){error(e);busy=false;render()}
}
$('#cancel').onclick=()=>{$('#preview').close();plan=null;busy=false;render()};$('#preview').addEventListener('cancel',e=>{if($('#execute').disabled&&plan?.count>0){e.preventDefault();return}plan=null;busy=false;render()});
$('#execute').onclick=async()=>{
 if(!plan||$('#execute').disabled)return;const confirmed=plan;$('#execute').disabled=true;$('#execute').textContent='正在提交…';$('#preview-error').textContent='正在提交；受理后在任务执行记录中持续显示进度。';
 try{
  const r=await post('/api/actions/execute',{plan:confirmed.plan,confirmation:$('#confirm-text').value},{timeoutMs:15000});
  job=r.job;plan=null;$('#preview').close();$('#job').textContent='操作已受理 · '+job+' · 正在分批执行，无执行到期时间。';checkJob();
 }catch(e){$('#preview-error').textContent=e.message;plan=null;$('#execute').disabled=true;$('#execute').textContent='请关闭并重新预览';busy=false}
};
let checkingJob=false,refreshing=false;
async function checkJob(){
 if(!job||checkingJob)return;checkingJob=true;
 try{
  const r=await post('/api/actions/status',{job},{timeoutMs:15000});
  $('#job').textContent=`${machineName(r.host)} · ${r.command} · ${r.state==='running'?(r.phase==='waiting'?'等待该机器当前操作结束':'分批执行中'):r.state==='done'?'执行完毕':'已停止，部分任务未执行'} ${r.processed??r.results.length}/${r.total} · 已过 ${Math.max(0,Math.floor(Date.now()/1000-r.created_at))} 秒\n`+r.results.slice(-30).map(t=>`#${t.id} ${t.name}：${outcomes[t.outcome]||t.outcome}`).join('\n');
  if(r.state!=='running'){job=null;busy=false;refresh()}
 }catch(e){error(e)}finally{checkingJob=false}
}
async function refresh(){
 if(stopped||refreshing)return;refreshing=true;const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),60000);
 try{
  const r=await fetch('/api/status',{signal:controller.signal});if(r.status===401){location.replace('/login');return}if(!r.ok)throw Error('监控服务不可用');data=await r.json();
  const selected=$('#host').value;$('#host').replaceChildren(new Option('所有主机',''),...data.hosts.map(h=>new Option(h.name||h.id,h.id)));$('#host').value=data.hosts.some(h=>h.id===selected)?selected:(data.hosts.length===1?data.hosts[0].id:'');
  if(!job){const running=(data.manual_jobs||[]).find(j=>j.state==='running');if(running){job=running.id;busy=true;checkJob()}}
  render();
 }catch(e){
  const message=e.name==='AbortError'?'状态加载超时，将自动重试；请检查网络连接。':e.message||String(e);
  error(Error(message));if(!data)$('#summary').textContent='暂未加载到机器信息，正在自动重试…';
 }finally{clearTimeout(timer);refreshing=false}
}
$('#task-view').onchange=()=>{page=0;render()};$('#host').onchange=()=>{page=0;render();syncAutomation(true)};$('#filter').onchange=$('#search').oninput=()=>{page=0;render()};$('#prev').onclick=()=>{page--;render()};$('#next').onclick=()=>{page++;render()};$('#detail-close').onclick=()=>$('#detail').close();document.querySelectorAll('#bulk button').forEach(b=>b.onclick=()=>preview($('#host').value,b.dataset.command));
$('#refresh').onclick=async()=>{try{if(!$('#host').value)throw Error('请选择一台主机');$('#refresh').disabled=true;await post('/api/refresh',{host:$('#host').value});await refresh()}catch(e){error(e)}finally{$('#refresh').disabled=false}};
(async()=>{try{const r=await fetch('/api/session');if(!r.ok){location.replace('/login');return}const session=await r.json();token=session.token;$('#account-user').value=session.username;$('#workspace-name').textContent=session.username+' 的工作空间';await refresh()}catch(e){error(e)}})();setInterval(()=>{if(!stopped){refresh();checkJob()}},5000);

let autoHost=null,autoBusy=false,probeResult=null,machineBusy=false;
function selectedMachine(){return data?.hosts.find(h=>h.id===$('#host').value)}
function syncAutomation(force){const h=selectedMachine();for(const id of ['edit-machine','delete-machine'])$('#'+id).disabled=!h;$('#auto-title').textContent=h?'自动清理设置 · '+machineName(h.id):'选择一台机器后设置自动清理';$('#auto-state').textContent=h?(h.auto_enabled?'当前已开启':'当前已关闭')+' · 连续零速 '+h.idle_seconds+' 秒':'';if(h&&(force||autoHost!==h.id))$('#idle-minutes').value=h.idle_seconds/60;autoHost=h?.id||null;for(const id of ['auto-on','auto-off','auto-save','idle-minutes'])$('#'+id).disabled=!h||autoBusy;}
async function setAuto(enabled){const h=selectedMachine();if(!h)return;const seconds=Number($('#idle-minutes').value)*60;if(!Number.isInteger(seconds)||seconds<data.poll_seconds*2||seconds>86400){error(Error('请输入有效时间：至少 '+data.poll_seconds*2+' 秒，最多 24 小时'));return}if(enabled&&!h.auto_enabled&&!confirm('为 '+machineName(h.id)+' 开启自动清理？连续零速达到 '+seconds+' 秒后会移除正在下载的任务，保留文件。'))return;autoBusy=true;syncAutomation(false);try{await post('/api/settings/automation',{host:h.id,enabled,idle_seconds:seconds});$('#error').textContent='设置已保存，'+machineName(h.id)+' 重新开始零速计时';await refresh();syncAutomation(true)}catch(e){error(e)}finally{autoBusy=false;syncAutomation(false)}}
$('#auto-on').onclick=()=>setAuto(true);$('#auto-off').onclick=()=>setAuto(false);$('#auto-save').onclick=()=>setAuto(selectedMachine()?.auto_enabled===true);
let machineEditing=null,deleteTarget=null,deleteBusy=false;
function machineNeedsProbe(){return !machineEditing||$('#machine-address').value.trim()!==machineEditing.address||Number($('#machine-port').value)!==machineEditing.port}
function machineCanSave(){return !machineNeedsProbe()||!!probeResult&&$('#machine-trust').checked}
function resetProbe(){probeResult=null;$('#machine-fingerprint').textContent='';$('#trust-label').hidden=true;$('#machine-trust').checked=false;$('#machine-save').disabled=machineBusy||!machineCanSave()}
function closeMachine(){if(machineBusy)return;$('#machine-dialog').close();$('#machine-password').value='';machineEditing=null;resetProbe()}
function openMachine(h=null){
 if(machineBusy)return;
 machineEditing=h?{id:h.id,address:h.address,port:h.port||22,name:machineName(h.id)}:null;
 $('#machine-title').textContent=h?'编辑机器 · '+machineName(h.id):'添加机器';
 $('#machine-description').textContent=h?'修改名称、IP 地址或 SSH 端口。保留现有 SSH 凭据；修改地址或端口需重新核对主机指纹，保存后自动检测连接。':'填写 SSH 连接信息。新机器默认关闭自动清理，添加后立即检测迅雷接口。';
 $('#machine-error').textContent='';$('#machine-id').value=h?machineName(h.id):'';$('#machine-address').value=h?.address||'';$('#machine-port').value=h?.port||22;$('#machine-user').value='';$('#machine-password').value='';
 $('#credential-source-label').hidden=!!h;
 $('#credential-source').replaceChildren(new Option('输入新的账号密码',''),...(data?.hosts||[]).map(h=>new Option('使用 '+machineName(h.id)+' 的账号密码',h.id)));
 $('#credential-source').value=h?'':selectedMachine()?.id||'';$('#credential-source').onchange();
 $('#machine-save').textContent=h?'保存修改':'添加并检测迅雷';resetProbe();$('#machine-dialog').showModal();
}
$('#add-machine').onclick=()=>openMachine();
$('#edit-machine').onclick=()=>{const h=selectedMachine();if(h)openMachine(h)};
$('#delete-machine').onclick=()=>{const h=selectedMachine();if(h)openDeleteMachine(h)};
$('#credential-source').onchange=()=>{$('#new-credentials').hidden=!!machineEditing||!!$('#credential-source').value};
for(const id of ['machine-id','machine-address','machine-port'])$('#'+id).oninput=resetProbe;
$('#machine-trust').onchange=()=>{$('#machine-save').disabled=machineBusy||!machineCanSave()};
function machineLock(value){machineBusy=value;for(const id of ['machine-probe','machine-cancel','machine-id','machine-address','machine-port','credential-source','machine-user','machine-password','machine-trust'])$('#'+id).disabled=value;$('#machine-save').disabled=value||!machineCanSave()}
$('#machine-probe').onclick=async()=>{
 resetProbe();machineLock(true);$('#machine-error').textContent='正在连接 SSH…';
 try{probeResult=await post('/api/hosts/probe',{id:$('#machine-id').value.trim(),host:machineEditing?.id,address:$('#machine-address').value.trim(),port:Number($('#machine-port').value)},{timeoutMs:30000});$('#machine-fingerprint').textContent=probeResult.address+':'+probeResult.port+'\n'+probeResult.fingerprint+'\n'+(probeResult.known?'与已保存的密钥一致':'首次连接，请核对指纹');$('#trust-label').hidden=false;$('#machine-error').textContent=''}catch(e){$('#machine-error').textContent=e.message}finally{machineLock(false)}
};
$('#machine-save').onclick=async()=>{
 if(!machineCanSave())return;machineLock(true);const editing=machineEditing;
 $('#machine-error').textContent=editing?'正在保存修改…':'正在添加并检测迅雷接口…';
 try{
  const body={probe:probeResult?.probe,trust:$('#machine-trust').checked};
  const r=editing?await post('/api/hosts/update',{...body,host:editing.id,name:$('#machine-id').value.trim(),address:$('#machine-address').value.trim(),port:Number($('#machine-port').value)},{timeoutMs:15000}):await post('/api/hosts/add',{...body,credentials_from:$('#credential-source').value,username:$('#machine-user').value,password:$('#machine-password').value});
  $('#machine-password').value='';machineBusy=false;closeMachine();await refresh();$('#host').value=r.id;render();syncAutomation(true);
  $('#error').textContent=editing?'机器设置已保存'+(editing.address!==r.address||editing.port!==r.port?'，正在等待连接检测':''):r.state==='在线'?'机器已添加，实时任务读取成功':'机器已保存，但连接或迅雷接口检测失败：'+r.error;
 }catch(e){$('#machine-error').textContent=e.message}finally{machineLock(false)}
};
$('#machine-cancel').onclick=closeMachine;$('#machine-dialog').addEventListener('cancel',e=>{if(machineBusy)e.preventDefault();else{$('#machine-password').value='';machineEditing=null;resetProbe()}});
function machineButtons(h){return [['编辑机器',()=>openMachine(h),''],['删除机器',()=>openDeleteMachine(h),'danger']].map(([label,action,cls])=>{const b=node('button',label,cls);b.onclick=action;return b})}
function openDeleteMachine(h){deleteTarget={id:h.id,name:machineName(h.id)};$('#machine-delete-description').textContent='将从你的工作空间移除「'+deleteTarget.name+'」（'+h.address+':'+(h.port||22)+'）。';$('#machine-delete-confirm').value='';$('#machine-delete-error').textContent='';$('#machine-delete-dialog').showModal()}
$('#machine-delete-cancel').onclick=()=>{if(!deleteBusy)$('#machine-delete-dialog').close()};
$('#machine-delete-dialog').addEventListener('cancel',e=>{if(deleteBusy)e.preventDefault()});
$('#machine-delete-form').onsubmit=async e=>{
 e.preventDefault();if(deleteBusy||!deleteTarget)return;deleteBusy=true;
 for(const id of ['machine-delete-submit','machine-delete-cancel','machine-delete-confirm'])$('#'+id).disabled=true;
 $('#machine-delete-error').textContent='正在移除连接配置…';
 try{await post('/api/hosts/delete',{host:deleteTarget.id,confirmation:$('#machine-delete-confirm').value},{timeoutMs:15000});$('#machine-delete-dialog').close();if($('#host').value===deleteTarget.id)$('#host').value='';await refresh();$('#error').textContent='机器已从当前工作空间移除，远端任务与文件保留'}catch(e){$('#machine-delete-error').textContent=e.message}finally{deleteBusy=false;for(const id of ['machine-delete-submit','machine-delete-cancel','machine-delete-confirm'])$('#'+id).disabled=false}
};

function showPage(name){$('#dashboard').hidden=name!=='dashboard';$('#task-page').hidden=name!=='tasks';$('#page-title').textContent=name==='dashboard'?'总览大盘':'任务管理';$('#breadcrumb').textContent=name==='dashboard'?'总览大盘':'任务管理';$('#tab-dashboard').classList.toggle('selected',name==='dashboard');$('#tab-tasks').classList.toggle('selected',name==='tasks')}
$('#tab-dashboard').onclick=()=>showPage('dashboard');$('#tab-tasks').onclick=()=>showPage('tasks');
const svgNS='http://www.w3.org/2000/svg';
function svgEl(name,attrs){const e=document.createElementNS(svgNS,name);for(const [k,v] of Object.entries(attrs))e.setAttribute(k,v);return e}
function plot(container,points,large=false){container.replaceChildren();const w=large?760:260,h=large?220:44,pad=large?45:2,svg=svgEl('svg',{viewBox:`0 0 ${w} ${h}`,role:'img','aria-label':'下载速度采样趋势'});container.append(svg);const values=points.filter(p=>p.speed!==null);if(!values.length){container.append(node('span','等待有效采样','muted'));return}const max=Math.max(1,...values.map(p=>p.speed));const first=points[0].time,last=Math.max(first+1,points[points.length-1].time);if(large){for(let i=0;i<4;i++){const y=12+i*(h-pad-12)/3;svg.append(svgEl('line',{x1:pad,x2:w-8,y1:y,y2:y,stroke:'#edf1f7'}));const label=svgEl('text',{x:0,y:y+4});label.textContent=fmt(max*(1-i/3));svg.append(label)}for(const [x,t] of [[pad,first],[w-70,last]]){const label=svgEl('text',{x,y:h-6});label.textContent=new Date(t*1000).toLocaleTimeString();svg.append(label)}}let path='',previous=null;for(const p of points){if(p.speed===null){previous=null;continue}const x=pad+(p.time-first)/(last-first)*(w-pad-8),y=12+(1-p.speed/max)*(h-pad-12);const gap=previous&&p.time-previous.time>Math.max(60,(data?.poll_seconds||15)*3);path+=(previous&&!gap?'L':'M')+x.toFixed(2)+','+y.toFixed(2);previous=p;}svg.append(svgEl('path',{d:path,fill:'none',stroke:'#5278ef','stroke-width':large?2.6:2,'stroke-linecap':'round','stroke-linejoin':'round'}));if(values.length===1)svg.append(svgEl('circle',{cx:pad,cy:12+(1-values[0].speed/max)*(h-pad-12),r:3,fill:'#5278ef'}));}
function renderDashboard(){const f=data.fleet;if(!f)return;const metrics=$('#metrics');metrics.replaceChildren();for(const [label,value,note,view] of [['总下载速度',f.known_speeds?fmt(f.speed)+'/s':'—',`${f.known_speeds}/${f.total} 台速度可用 · 异常不计为零速`],['在线机器',f.online+' / '+f.total,'基于最近一次有效采样'],['正在下载',f.downloading,'实时下载列表中的任务'],['当前零速任务',f.zero,'仅正在下载 · 排队 '+f.queued+' 个'],['已完成任务',f.completed_known||!f.total?String(f.completed||0):'—',`${f.completed_known||0}/${f.total} 台已读取 · 点击查看`,'completed'],['回收站',f.recycle_known||!f.total?String(f.recycle||0):'—',`${f.recycle_known||0}/${f.total} 台已读取 · 仅查看`,'recycle']]){const card=node('div','','metric');const title=node('div',label,'label');title.prepend(node('i','','dot'));card.append(title,node('strong',value),node('small',note));if(view){card.classList.add('archive-metric');card.role='button';card.tabIndex=0;card.onclick=()=>selectTaskView(view);card.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();selectTaskView(view)}}}metrics.append(card)}
const histories=Object.values(f.history);const times=[...new Set(histories.flatMap(a=>a.map(p=>p.time)))].sort((a,b)=>a-b).slice(-240);const points=times.map(t=>{const values=histories.map(a=>{for(let i=a.length-1;i>=0;i--)if(a[i].time<=t)return t-a[i].time<=Math.max(60,data.poll_seconds*3)?a[i].speed:null;return null});return {time:t,speed:values.some(v=>v!==null)?values.reduce((n,v)=>n+(v||0),0):null}});plot($('#speed-chart'),points,true);$('#chart-caption').textContent='有效机器速度之和；断线时留空或只统计其余机器。历史仅保留本次运行最近 240 个采样点。';$('#chart-range').textContent=points.length>1?'最近 '+Math.max(1,Math.round((points.at(-1).time-points[0].time)/60))+' 分钟':'等待更多采样';
const dist=$('#task-distribution');dist.replaceChildren();const transmitting=f.transmitting,den=Math.max(1,f.tasks||f.downloading+f.queued);for(const [label,count,color] of [['正在传输',transmitting,'#5278ef'],['下载中零速',f.zero,'#ecb65e'],['排队等待',f.queued,'#a8b7d7'],['暂停',f.paused||0,'#a48ad4'],['其他',f.other||0,'#d5d9e3'],['速度未知',Math.max(0,f.downloading-f.transmitting-f.zero),'#c6d0df']]){const row=node('div','','distribution-row'),heading=node('div','');heading.append(node('span',label),node('strong',String(count)));const bar=node('div','','bar'),fill=node('i','');fill.style.width=(count/den*100)+'%';fill.style.background=color;bar.append(fill);row.append(heading,bar);dist.append(row)}
const grid=$('#machine-grid');grid.replaceChildren();const side=$('#sidebar-machines');side.replaceChildren();$('#sidebar-count').textContent=String(data.hosts.length);const machineTable=node('table','');const head=node('tr','');['机器','连接','下载速度','任务分布','传输 / 零速 / 排队 / 暂停','操作'].forEach(t=>head.append(node('th',t)));machineTable.append(head);
for(const h of data.hosts){const stats=f.machines.find(m=>m.id===h.id);if(!stats)continue;
const open=()=>selectTaskView('incomplete',h.id);
const sb=node('button','','sidebar-machine');sb.classList.toggle('active',$('#host').value===h.id);sb.append(node('i','',stats.fresh?'connection-dot':'connection-dot offline'),node('span',machineName(h.id)),node('small',stats.speed===null?'—':fmt(stats.speed)+'/s'));sb.onclick=open;side.append(sb);
const tile=node('article','','machine-tile');const top=node('div','','machine-top');top.append(node('strong','▣  '+machineName(h.id)),node('span',stats.fresh?'● 在线':'● 待检查',stats.fresh?'status-dot':'status-dot offline'));tile.append(top,node('div',h.address,'muted'));const speed=node('div',stats.speed===null?'—':fmt(stats.speed)+'/s','machine-speed');speed.append(node('small','下载速度'));tile.append(speed);const spark=node('div','','machine-spark');plot(spark,f.history[h.id]||[]);tile.append(spark);
const buckets=machineBuckets(stats);tile.append(distributionStack(buckets));const legend=node('div','','machine-legend');for(const [label,count,color] of buckets){const item=node('span','');const dot=node('i','');dot.style.background=color;item.append(dot,node('span',label),node('strong',stats.fresh?String(count):'—'));legend.append(item)}const archiveLinks=node('div','','archive-links');for(const view of ['completed','recycle']){const link=node('button',(view==='recycle'?'回收站 ':'已完成 ')+archiveCount(h,view));link.onclick=()=>selectTaskView(view,h.id);archiveLinks.append(link)}tile.append(archiveLinks);tile.append(legend,node('div',h.error||(h.auto_enabled?'自动清理开启 · '+h.idle_seconds+' 秒':'自动清理关闭'),'muted'));const actions=node('div','','machine-actions');const manage=node('button','任务管理 ↗');manage.onclick=open;const settings=node('button','自动清理设置');settings.onclick=()=>{open();$('#auto-title').scrollIntoView({behavior:'smooth',block:'center'})};const inspect=node('button','零速任务');inspect.onclick=()=>{open();$('#filter').value='zero';render()};actions.append(manage,inspect,settings,...machineButtons(h));tile.append(actions);grid.append(tile);
const tr=node('tr','');[machineName(h.id),stats.fresh?'在线':'待检查',stats.speed===null?'—':fmt(stats.speed)+'/s'].forEach(v=>tr.append(node('td',v)));const barCell=node('td','');barCell.append(distributionStack(buckets));tr.append(barCell,node('td',stats.fresh?[stats.transmitting,stats.zero,stats.queued,stats.paused||0].join(' / '):'—'));const cell=node('td',''),button=node('button','查看任务');button.onclick=open;const recycleButton=node('button','回收站 '+archiveCount(h,'recycle'));recycleButton.onclick=()=>selectTaskView('recycle',h.id);cell.append(button,recycleButton,...machineButtons(h));tr.append(cell);machineTable.append(tr)}$('#machine-table').replaceChildren(machineTable);$('#chart-live-speed').textContent=f.known_speeds?fmt(f.speed)+'/s':'—';renderRing(f);

renderCloudStatus();const run=f.acceleration;$('#accelerate').disabled=data.hosts.length===0||(!!run&&run.state==='running');$('#accelerate').textContent=run?.state==='running'?'加速执行中…':'⚡ 一键加速';const out=$('#acceleration-results');out.replaceChildren();if(!run){out.append(node('span','暂无执行记录','muted'));return}const table=node('table','');const tr=node('tr','');['机器','提交状态','匹配','已提交转存','请求状态'].forEach(x=>tr.append(node('th',x)));table.append(tr);for(const [id,r] of Object.entries(run.hosts)){const row=node('tr',''),job=(data.cloud?.jobs||[]).find(j=>j.id===r.job);[machineName(id),({waiting:'等待',checking:'检查中',submitted:'已提交',done:'无待转存项',stopped:'已停止',error:r.error||'提交失败'}[r.state]||r.state),r.matched,r.submitted||0,job?cloudJobLabel(job)+(job.total?' '+(job.processed||0)+'/'+job.total:''):'—'].forEach(x=>row.append(node('td',String(x))));table.append(row)}out.append(table);}
$('#accelerate').onclick=()=>openCloud('fleet');


function machineBuckets(s){return [['传输中',s.transmitting||0,'#4096f5'],['零速',s.zero||0,'#edb45d'],['排队',s.queued||0,'#a7b8d8'],['暂停',s.paused||0,'#aa93cf'],['其他',s.other||0,'#d1d6df'],['速度未知',Math.max(0,(s.downloading||0)-(s.transmitting||0)-(s.zero||0)),'#c6d0df']]}
function distributionStack(buckets){const bar=node('div','','machine-stack');const total=buckets.reduce((n,b)=>n+b[1],0);bar.title=buckets.map(b=>b[0]+' '+b[1]).join(' · ');for(const [label,count,color] of buckets){const seg=node('i','');seg.style.width=(total?count/total*100:0)+'%';seg.style.background=color;bar.append(seg)}return bar}
function renderRing(f){const container=$('#distribution-ring');container.replaceChildren();const wrap=node('div','','ring-wrap'),svg=svgEl('svg',{viewBox:'0 0 130 130',role:'img','aria-label':'全部未完成任务分布'}),total=f.tasks??f.downloading+f.queued,perimeter=2*Math.PI*52;svg.append(svgEl('circle',{cx:65,cy:65,r:52,fill:'none',stroke:'#eef0f5','stroke-width':12}));let offset=0;const buckets=[...machineBuckets(f)];for(const [label,count,color] of buckets){if(!count||!total)continue;const length=count/total*perimeter;svg.append(svgEl('circle',{cx:65,cy:65,r:52,fill:'none',stroke:color,'stroke-width':12,'stroke-dasharray':`${length} ${perimeter-length}`,'stroke-dashoffset':-offset}));offset+=length}const center=node('div','','ring-center');center.append(node('strong',String(total)),node('small','未完成任务'));wrap.append(svg,center);container.append(wrap)}
$('#empty-add').onclick=$('#top-add').onclick=$('#sidebar-add').onclick=()=>$('#add-machine').click();$('#top-refresh').onclick=refresh;
$('#view-cards').onclick=()=>{$('#machine-grid').hidden=false;$('#machine-table').hidden=true;$('#view-cards').classList.add('selected');$('#view-table').classList.remove('selected')};
$('#view-table').onclick=()=>{$('#machine-grid').hidden=true;$('#machine-table').hidden=false;$('#view-table').classList.add('selected');$('#view-cards').classList.remove('selected')};

$('#logout').onclick=async()=>{try{await post('/api/auth/logout',{});location.replace('/login')}catch(e){error(e)}};
$('#account-open').onclick=()=>{$('#account-old').value=$('#account-new').value='';$('#account-error').textContent='';$('#account-dialog').showModal()};
$('#account-close').onclick=()=>$('#account-dialog').close();
$('#account-form').onsubmit=async e=>{e.preventDefault();try{await post('/api/auth/password',{username:$('#account-user').value,old_password:$('#account-old').value,password:$('#account-new').value});location.replace('/login')}catch(e){$('#account-error').textContent=e.message}};
$('#credentials-open').onclick=()=>{if(!selectedMachine()){error(Error('请先选择一台机器'));return}$('#credentials-user').value=$('#credentials-password').value='';$('#credentials-error').textContent='';$('#credentials-dialog').showModal()};
$('#credentials-close').onclick=()=>$('#credentials-dialog').close();
$('#credentials-form').onsubmit=async e=>{e.preventDefault();try{await post('/api/settings/credentials',{host:selectedMachine()?.id,username:$('#credentials-user').value,password:$('#credentials-password').value});$('#credentials-password').value='';$('#credentials-dialog').close();await refresh()}catch(e){$('#credentials-error').textContent=e.message}};

const cloudStages={waiting_for_machine:'等待机器当前操作结束',launching:'正在连接并启动远端工作',waiting_for_worker:'已登记，等待远端执行',initializing:'远端正在初始化',waiting_for_mutex:'等待其他云盘操作结束',connecting_client:'正在连接迅雷',submitted:'缓存提交阶段已完成',dispatching:'准备提交',running:'正在执行',recovering:'正在核对重启前记录',uncertain:'执行结果待核对',done:'提交处理完毕',error:'执行失败',submitting:'正在缓存',prepared:'原任务已保存',deleted:'原任务已移除·文件保留',caching:'云缓存中',cacheOnly:'仅缓存',waiting:'等待到点检查',checking:'正在检查完整缓存',checked:'本次检查已结束',manual:'待下次手动处理',empty:'无匹配项',ready:'等待建单',created:'本地任务已创建',started:'已收到下载数据',moved:'已归入“已下载”',alreadyLocal:'原文件已在本地',review:'需要人工核对',unselected:'原 BT 未勾选',ambiguous:'文件对应不明确',external:'已有其他下载'};
function cloudJobLabel(j){return cloudStages[['error','uncertain','recovering'].includes(j.state)?j.state:j.phase]||cloudStages[j.state]||j.state}
let cloudContext=null,cloudPlan=null,cloudWorking=false;
function cloudEligible(t){return !t.is_pan_task&&t.origin!=='cloud_bridge_v1'&&[1,2,5].includes(t.task_type)&&!!t.save_path&&(!!t.url||[2,5].includes(t.task_type))}
function cloudOptions(){return {progress_limit:Number($('#cloud-limit').value),wait_minutes:Number($('#cloud-wait').value),output:$('#cloud-output').value.trim(),folder:cloudContext?.command==='download'?$('#cloud-folder').value.trim():'',cache_only:$('#cloud-cache-only').checked}}
function cloudInvalidate(){cloudPlan=null;$('#cloud-submit').disabled=true;$('#cloud-preview-list').replaceChildren()}
function cloudLock(on){cloudWorking=on;for(const id of ['cloud-preview','cloud-list','cloud-status','cloud-install','cloud-limit','cloud-wait','cloud-output','cloud-folder','cloud-cache-only'])$('#'+id).disabled=on;$('#cloud-submit').disabled=on||!cloudPlan;}
function openCloud(command,task=null){
 const host=task?.host||$('#host').value;
 if(command!=='fleet'&&!host){error(Error('请先选择一台机器'));return}
 cloudContext={command,host,task};cloudInvalidate();$('#cloud-error').textContent='';$('#cloud-resources').replaceChildren();
 const downloading=command==='download';$('#cloud-advanced').open=downloading;$('#cloud-folder-label').hidden=!downloading;$('#cloud-output-hint').textContent=downloading?'目标 Windows 保存路径（必填）':'本地下载位置（可选）';
 if(!downloading){$('#cloud-output').value='';$('#cloud-folder').value=''}
 $('#cloud-auto-note').textContent=downloading?'请选择本地保存路径。云盘目录 ID 留空时，读取本工作空间自动创建的转存目录。':'默认自动创建并复用云盘根目录，每批创建独立子目录；本地下载沿用各原任务目录，无需填写路径或目录 ID。';
 $('#cloud-title').textContent=command==='fleet'?'一键云盘加速 · 所有机器':command==='recycle'?'回收站转存 · '+machineName(host):command==='download'?'云盘下载 · '+machineName(host):command==='sync'?'核对已有下载 · '+machineName(host):'云盘转存 · '+machineName(host);
 $('#cloud-description').textContent=command==='fleet'?'预览每台机器的零速任务，确认后转存云盘并安排本地下载。':command==='recycle'?(task?'仅转存任务 #'+task.id+'；确认云盘缓存完成后，清理对应回收站记录并保留本地文件。':'转存此机器可用的回收站任务；逐项确认云盘缓存完成后清理对应回收站记录，保留本地文件。失败或结果不明的条目保留。'):command==='download'?'预览指定云盘目录中整项缓存完成的资源，再创建本地下载。':command==='sync'?'仅核对已创建的本地下载，实际收到数据后将云盘文件归档；不重新创建下载。':'将此机器低于进度门槛的零速普通下载转存云盘。';
 for(const id of ['cloud-list','cloud-status','cloud-install','cloud-sync'])$('#'+id).hidden=command==='fleet';
 $('#cloud-dialog').showModal();renderCloudStatus();
}
$('#cloud-open').onclick=$('#cloud-transfer').onclick=()=>openCloud('transfer');
$('#cloud-recycle').onclick=()=>openCloud('recycle');$('#cloud-download').onclick=()=>openCloud('download');
$('#cloud-close').onclick=()=>{$('#cloud-dialog').close();cloudContext=null;cloudInvalidate()};
$('#cloud-dialog').addEventListener('cancel',()=>{cloudContext=null;cloudInvalidate()});
for(const id of ['cloud-limit','cloud-wait','cloud-output','cloud-folder','cloud-cache-only'])$('#'+id).oninput=cloudInvalidate;
$('#cloud-preview').onclick=async()=>{
 if(!cloudContext||cloudWorking)return;cloudLock(true);cloudInvalidate();$('#cloud-error').textContent='正在并行读取各机器候选项…';
 try{
  const opts=cloudOptions(),ctx=cloudContext;
  const hosts=ctx.command==='fleet'?data.hosts.map(h=>h.id):[ctx.host];
  const list=$('#cloud-preview-list');let finished=0;
  const boxes=hosts.map(host=>{const box=node('div','','cloud-status-item');box.append(node('h3',machineName(host)+' · 正在读取…'));list.append(box);return box});
  const replies=new Array(hosts.length);let next=0;
  async function readNext(){while(next<hosts.length){
   const i=next++,host=hosts[i];let reply;const started=Date.now();
   try{reply={host,plan:await requestPreview({kind:'cloud',...opts,host,command:ctx.command==='fleet'?'transfer':ctx.command,...(ctx.command==='recycle'?{cleanup_recycle:true,...(ctx.task?{task_ids:[ctx.task.id]}:{all:true})}:{})},message=>{if(ctx===cloudContext)boxes[i].replaceChildren(node('h3',machineName(host)+' · '+message))})}}
   catch(e){reply={host,error:e.message}}
   if(ctx===cloudContext){const box=boxes[i];box.replaceChildren(node('h3',machineName(host)+' · '+(reply.error?reply.error:reply.plan.count+' 项')+' · '+((Date.now()-started)/1000).toFixed(1)+' 秒'));if(reply.plan){for(const t of reply.plan.tasks)box.append(node('p','#'+t.id+' '+t.name+'\n'+t.save_path));for(const t of reply.plan.resources)box.append(node('p',t.name+' · '+t.remaining+' 个待下载文件'))}finished++;$('#cloud-error').textContent=`已读取 ${finished}/${hosts.length} 台机器…`;}
   replies[i]=reply;
  }}
  await Promise.all([readNext(),readNext()]);
  if(ctx!==cloudContext)return;
  const count=replies.reduce((n,r)=>n+(r.plan?.count||0),0);
  cloudPlan={context:ctx,options:opts,replies:replies.filter(r=>r.plan&&r.plan.count>0)};
  $('#cloud-error').textContent=count?`共 ${count} 项，预览 15 分钟内有效；受理后无此执行时限。确认后仅处理这些项目。`:'没有符合条件的项目，或存在读取失败；未执行操作。';
  if(!count)cloudPlan=null;
 }catch(e){$('#cloud-error').textContent=e.message}finally{cloudLock(false)}
};
$('#cloud-submit').onclick=async()=>{
 if(!cloudPlan||cloudWorking)return;const p=cloudPlan;cloudLock(true);cloudInvalidate();
 $('#cloud-submit').textContent='正在提交…';$('#cloud-error').textContent='正在提交已确认的任务，请稍候。无需等待云盘缓存完成即可收到受理结果。';
 try{
  const result=p.context.command==='fleet'
   ?await post('/api/fleet/accelerate',{...p.options,plans:p.replies.map(r=>({plan:r.plan.plan}))},{timeoutMs:15000})
   :await post('/api/cloud/execute',{plan:p.replies[0].plan.plan},{timeoutMs:15000});
  $('#cloud-error').textContent='服务已受理 · 编号 '+(result.run||result.job)+'。下方会自动更新各机器进度；关闭网页不影响后台执行。';
 }catch(e){$('#cloud-error').textContent=e.message}finally{cloudLock(false);$('#cloud-submit').textContent='确认执行';refresh()}
};

async function cloudInspect(action){
 if(!cloudContext||cloudContext.command==='fleet'||cloudWorking)return;cloudLock(true);$('#cloud-error').textContent='正在读取…';
 try{
  const result=await post('/api/cloud/inspect',{host:cloudContext.host,action,...cloudOptions()});
  if(action==='list'){const box=$('#cloud-resources');box.replaceChildren(node('h3','云盘资源 · '+(result.scope||'')));for(const r of result.resources||[])box.append(node('p',(r.name||r.id)+' · '+(r.ready?'整项缓存完成':'尚未完整缓存')+' · 待下载 '+(r.remaining||0)));if(!result.resources?.length)box.append(node('p','暂无本工作空间的待下载资源'))}
  $('#cloud-error').textContent=action==='install'?'云盘模块已安装／更新，未启动转存或下载。':'读取完成';await refresh();
 }catch(e){$('#cloud-error').textContent=e.message}finally{cloudLock(false)}
}
$('#cloud-list').onclick=()=>cloudInspect('list');$('#cloud-status').onclick=()=>cloudInspect('status');$('#cloud-install').onclick=()=>cloudInspect('install');
function renderCloudStatus(){
 const box=$('#cloud-status-view');if(!box)return;box.replaceChildren();
 const hosts=cloudContext?.host?[cloudContext.host]:data?.hosts.map(h=>h.id)||[];
 for(const id of hosts){const state=data?.cloud?.hosts?.[id],jobs=(data?.cloud?.jobs||[]).filter(j=>j.host===id);if(!state&&!jobs.length)continue;box.append(node('h3',machineName(id)+' · 转存状态'));
  if(state?.state==='error')box.append(node('p',state.error,'bad'));
  for(const j of jobs.slice(-10).reverse())box.append(node('p','请求 '+j.id.slice(0,8)+' · '+cloudJobLabel(j)+(j.total?' · '+(j.processed||0)+'/'+j.total+' 项':'')+(j.current_task?' · 当前 #'+j.current_task:'')+' · 已过 '+Math.max(0,Math.floor(Date.now()/1000-j.created_at))+' 秒'+(j.error?' · '+j.error:'')));
  for(const r of state?.requests||[]){const item=node('div','','cloud-status-item');item.append(node('strong','缓存批次 '+r.id.slice(0,8)+' · '+(cloudStages[r.stage]||r.stage)),node('p',(r.jobIds?.length||0)+' 项 · '+(r.cacheOnly?'仅缓存':r.notBefore?'检查时间 '+dateTime(r.notBefore):'尚未安排检查')+' · 失败 '+r.failed));for(const x of r.results||[])item.append(node('small',x.id+'：'+x.reason));box.append(item)}
  const cleanupLabels={waiting:'等待缓存完成',cleaned:'已清理回收站记录',absent:'原记录已不在回收站',changed:'原记录变化，保留待核对',uncertain:'清理结果待核对，不重复执行',expired:'检查已到期，原记录保留'};
  for(const r of state?.requests||[]){if(r.cleanupRecycle)box.append(node('p','回收站清理 · '+(r.cleanupStage==='done'?'检查完成':r.cleanupStage==='review'?'有条目需核对':'等待检查')+' · 已清理 '+(r.cleanupCleaned||0)+' · 等待 '+(r.cleanupPending||0)+' · 待核对 '+(r.cleanupReview||0)))}
  for(const j of state?.jobs||[]){const item=node('div','','cloud-status-item');item.append(node('strong',(j.name||j.id)+' · '+(cloudStages[j.stage]||j.stage)+(j.needs_review?' · 需要核对':'')));if(j.recycle_cleanup)item.append(node('p','回收站：'+(cleanupLabels[j.recycle_cleanup]||j.recycle_cleanup)+(j.recycle_cleanup_review?' · 需要核对':'')));for(const d of j.downloads||[])item.append(node('p',d.name+' · '+(cloudStages[d.stage]||d.stage)+(d.taskId?' · 本地 #'+d.taskId:'')+(d.needs_review?' · 需要核对':'')+'\n'+(d.savePath||'')));box.append(item)}
 }
 if(!box.children.length)box.append(node('p','暂无本工作空间的转存记录。可点击“刷新转存状态”读取。','muted'));
}

$('#cloud-sync').onclick=()=>{if(cloudContext?.host&&!cloudWorking){cloudContext.command='sync';cloudInvalidate();$('#cloud-title').textContent='核对已有下载 · '+cloudContext.host;$('#cloud-error').textContent='点击预览，核对本次已有下载并确认归档。'}};
