"""Snapshot-bound manual commands, matching the supplied Thunder CLI."""
import base64
import json
import math
import secrets
import threading
import time
from thunder_api import read_live_tasks, remote
from resource_limits import PLAN_SECONDS
from concurrent.futures import ThreadPoolExecutor, as_completed

COMMANDS = {
    'start_all': ('启动暂停、失败等未完成任务', 'start', False, False, True),
    'pause_zero': ('暂停当前零速任务', 'pause', False, False, True),
    'del': ('删除任务，保留文件', 'delete', False, False, False),
    'delf': ('删除任务及本地文件', 'delete', True, False, False),
    'del_zero': ('删除当前零速任务，保留文件', 'delete', False, False, True),
    'delf_zero': ('删除当前零速任务及文件', 'delete', True, False, True),
    'del_zero20': ('删除零速且进度小于 20% 的任务，保留文件', 'delete', False, True, True),
    'delf_zero20': ('删除零速且进度小于 20% 的任务及文件', 'delete', True, True, True),
}


def candidate(t, under20=False, progress_limit=None):
    zero = (t.get('read_only') is not True and t.get('view', 'incomplete') == 'incomplete'
            and not t.get('recycle_time', 0) and not t.get('is_pan_task') and t.get('origin') != 'cloud_bridge_v1' and t.get('active') is True
            and t.get('status') == 5 and type(t.get('speed')) is int and t['speed'] == 0)
    if not zero:
        return False
    limit = 20 if under20 else progress_limit
    return limit is None or (type(t.get('size')) is int and type(t.get('received')) is int
                            and t['size'] > 0 and 0 <= t['received'] * 100 < t['size'] * limit)


ACTION = r"""
$p=([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__')) | ConvertFrom-Json)
$id=[long]$p.id
function Test-ManualCurrent($t) {
 if($null -eq $t -or $t.taskId -ne $id -or $t.taskName -cne $p.name -or $t.savePath -cne $p.path){return $false}
 $currentIds=@((Invoke-ThunderApi $connection 'CategoryManagerCategoryViewGetTasks' @(-1,1)).data)
 if($t.recycleTime -gt 0 -or $id -notin $currentIds){return $false}
 if($p.mode -eq 'start'){return ($t.taskStatus -in @(1,2,7,9))}
 if($p.bulk){
  if($t.isPanTask -or $t.origin -eq 'cloud_bridge_v1'){return $false}
  $ids=@((Invoke-ThunderApi $connection 'CategoryManagerCategoryGetDownloadingTaskIds' @(-1)).data)
  if($id -notin $ids -or $t.taskStatus -ne 5 -or $null -eq $t.downloadSpeed -or $t.downloadSpeed -ne 0){return $false}
  if($p.under20 -and ($null -eq $t.fileSize -or $null -eq $t.downloadSize -or $t.fileSize -le 0 -or $t.downloadSize -lt 0 -or ([decimal]$t.downloadSize * 5) -ge [decimal]$t.fileSize)){return $false}
  if($null -ne $p.progressLimit -and ($null -eq $t.fileSize -or $null -eq $t.downloadSize -or $t.fileSize -le 0 -or $t.downloadSize -lt 0 -or ([decimal]$t.downloadSize * 100) -ge ([decimal]$t.fileSize * [decimal]$p.progressLimit))){return $false}
 }
 return $true
}
$t=(Invoke-ThunderApi $connection 'TaskManagerTaskGetTaskBase' @($id)).data
if(!(Test-ManualCurrent $t)){Write-CliResult @{outcome='skipped';reason='changed'};return}
if($p.mode -eq 'start'){
 $r=Invoke-ThunderApi $connection 'TaskManagerTaskStartTask' @($id)
 $after=(Invoke-ThunderApi $connection 'TaskManagerTaskGetTaskBase' @($id)).data
 if($after.taskId -ne $id -or $after.recycleTime -gt 0 -or $after.taskStatus -notin @(1,2,3,4,5,8)){throw '启动后状态未确认'}
 Write-CliResult @{outcome='started';taskId=$id;status=$after.taskStatus}
}elseif($p.mode -eq 'pause'){
 $null=Invoke-ThunderApi $connection 'TaskManagerTaskStopTask' @($id,7)
 $after=$null
 for($i=0;$i -lt 8;$i++){
  Start-Sleep -Milliseconds 250
  $after=(Invoke-ThunderApi $connection 'TaskManagerTaskGetTaskBase' @($id)).data
  if($after.taskId -eq $id -and $after.taskStatus -eq 7){break}
 }
 if($after.taskId -ne $id -or $after.taskStatus -ne 7){throw '暂停结果未确认'}
 Write-CliResult @{outcome='paused';taskId=$id;status=7}
}else{
 $result=(Invoke-ThunderApi $connection 'TaskManagerTaskDeleteTask' @($id,[bool]$p.files)).data
 if($null -eq $result -or $result -ne 0){throw '删除调用失败'}
 $after=(Invoke-ThunderApi $connection 'TaskManagerTaskGetTaskBase' @($id)).data
 if($after.taskId -eq $id){throw '删除结果未确认'}
 Write-CliResult @{outcome='deleted';taskId=$id;filesRequested=[bool]$p.files}
}
"""


def perform(host, command, task, data_dir=None, progress_limit=None):
    _, mode, files, under20, bulk = COMMANDS[command]
    payload = dict(id=task['id'], name=task['name'], path=task['save_path'], mode=mode, files=files, under20=under20, bulk=bulk, progressLimit=progress_limit)
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
    result = remote(host, ACTION.replace('__PAYLOAD__', encoded), data_dir)
    if result.get('outcome') not in ('paused', 'deleted', 'skipped', 'started'):
        raise RuntimeError('操作结果无法确认')
    if result['outcome'] == 'deleted' and (mode != 'delete' or result.get('filesRequested') is not files):
        raise RuntimeError('删除结果不匹配')
    if result['outcome'] == 'paused' and mode != 'pause':
        raise RuntimeError('暂停结果不匹配')
    if result['outcome'] == 'started' and mode != 'start':
        raise RuntimeError('启动结果不匹配')
    return result


def perform_many(host, command, tasks, data_dir=None, progress_limit=None):
    """One SSH / PowerShell / native connection for a small frozen chunk."""
    _, mode, files, under20, bulk = COMMANDS[command]
    items = [dict(id=t['id'], name=t['name'], path=t['save_path'], mode=mode,
                  files=files, under20=under20, bulk=bulk, progressLimit=progress_limit) for t in tasks]
    encoded = base64.b64encode(json.dumps(items, ensure_ascii=False).encode()).decode()
    body = ACTION[ACTION.index('$id=[long]$p.id'):].replace('Write-CliResult ', 'return ')
    script = r"""
$items=([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__ITEMS__'))|ConvertFrom-Json)
function Invoke-ManualOne($p) {
__BODY__
}
$results=@()
foreach($p in @($items)) {
 try{$value=Invoke-ManualOne $p;$value.id=[long]$p.id}
 catch{$value=@{id=[long]$p.id;outcome='uncertain';reason='请核对目标机实际状态，不自动重试'}}
 $results+=,$value
 if($value.outcome -eq 'uncertain'){break}
}
Write-CliResult @{results=$results}
""".replace('__ITEMS__', encoded).replace('__BODY__', body)
    result = remote(host, script, data_dir).get('results')
    if not isinstance(result, list) or not 0 < len(result) <= len(tasks):
        raise RuntimeError('批量结果无法确认')
    expected = {'start': 'started', 'pause': 'paused', 'delete': 'deleted'}[mode]
    for row, task in zip(result, tasks):
        if row.get('id') != task['id'] or row.get('outcome') not in (expected, 'skipped', 'uncertain'):
            raise RuntimeError('批量任务身份或结果不匹配')
        if row['outcome'] == 'deleted' and row.get('filesRequested') is not files:
            raise RuntimeError('批量删除结果不匹配')
    if len(result) < len(tasks) and result[-1]['outcome'] != 'uncertain':
        raise RuntimeError('批量结果不完整')
    return result


class ManualActions:
    def __init__(self, monitor):
        self.monitor = monitor
        self.lock = threading.Lock()
        self.plans = {}
        self.jobs = {}
        self.busy = set()
        self.threads = []

    def host(self, host_id):
        return next((h for h in self.monitor.config['hosts'] if h['id'] == host_id), None)

    def prepare(self, host_id, command, task_id=None, progress_limit=None):
        if self.monitor.stop.is_set():
            raise ValueError('服务正在关闭')
        host = self.host(host_id)
        if progress_limit is not None and (type(progress_limit) not in (int,float) or not math.isfinite(progress_limit) or not 0 <= progress_limit <= 100):
            raise ValueError('进度门槛须为 0–100%')
        if host is None or command not in COMMANDS:
            raise ValueError('主机或命令无效')
        with self.lock:
            if host_id in self.busy or self.monitor.cloud.is_busy(host_id):
                raise ValueError('该主机正在执行操作，请等待完成')
            self.plans = {k:v for k,v in self.plans.items() if time.monotonic() < v['expires']}
            if len(self.plans) >= 30:
                raise ValueError('待确认操作过多，请等待过期')
        _, _, files, under20, bulk = COMMANDS[command]
        if not bulk and (type(task_id) is not int or task_id < 0):
            raise ValueError('请指定任务 ID')
        with self.monitor.lock:
            cached = self.monitor.hosts.get(host_id, {})
            fresh = cached.get('state') == '在线' and time.time() - cached.get('checked_at', 0) < 60
            rows = list(cached.get('tasks', [])) if fresh else None
        if rows is None:
            rows = read_live_tasks(host, self.monitor.data_dir, active_only=command != 'start_all' and bulk)
        rows = [t for t in rows if not t.get('read_only') and t.get('view', 'incomplete') == 'incomplete' and not t.get('recycle_time', 0)]
        if command == 'start_all':
            tasks = [t for t in rows if t.get('status') in (1,2,7,9)]
        else:
            tasks = [t for t in rows if candidate(t,under20,progress_limit) and not self.monitor.cloud.protected(host_id,t['id'])] if bulk else [t for t in rows if t['id'] == task_id]
        if not bulk and not tasks:
            raise ValueError('任务不存在或已完成，请刷新列表')
        public = dict(host=host_id, command=command, label=COMMANDS[command][0], files=files, count=len(tasks),
                      tasks=[dict(id=t['id'], name=t['name'], path=t['save_path'], size=t['size'], received=t['received']) for t in tasks],
                      expires_seconds=PLAN_SECONDS)
        if progress_limit is not None and command in ('pause_zero','del_zero','delf_zero'):
            public['label'] += f'（进度 <{progress_limit:g}%）'
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.plans[token] = dict(host=host, command=command, tasks=tasks, progress_limit=progress_limit, expires=time.monotonic()+PLAN_SECONDS)
        return {**public, 'plan':token}

    def execute(self, token, confirmation):
        with self.lock:
            plan = self.plans.get(token)
            if not plan or time.monotonic() >= plan['expires']:
                raise ValueError('预览已过期，请重新预览')
            host_id = plan['host']['id']
            if self.host(host_id) is not plan['host']:
                raise ValueError('机器配置已变化，请重新预览')
            if self.monitor.stop.is_set() or host_id in self.busy or self.monitor.cloud.is_busy(host_id):
                raise ValueError('服务正在关闭或该主机正在执行操作')
            if COMMANDS[plan['command']][2] and confirmation != '删除文件':
                raise ValueError('删除本地文件需要输入“删除文件”确认')
            self.plans.pop(token)  # single use, also on failure
            self.busy.add(host_id)
            job_id = secrets.token_hex(12)
            self.jobs[job_id] = dict(id=job_id,host=host_id,command=plan['command'],state='running',phase='waiting',created_at=time.time(),total=len(plan['tasks']),results=[])
            # Keep only recent completed jobs.
            old = [k for k,v in self.jobs.items() if v['state'] != 'running']
            for k in old[:-9]:
                self.jobs.pop(k)
            worker = threading.Thread(target=self._run, args=(job_id,plan),daemon=True)
            self.threads = [t for t in self.threads if t.is_alive()] + [worker]
            worker.start()
            return job_id

    def _run(self, job_id, plan):
        host,command = plan['host'],plan['command']
        host_id=host['id']
        try:
            with self.monitor.operation_locks[host_id]:
                with self.lock:
                    self.jobs[job_id]['phase'] = 'executing'
                if command == 'start_all' or len(plan['tasks']) > 8:
                    self._run_chunks(job_id, plan)
                    return
                for task in plan['tasks']:
                    if self.monitor.stop.is_set():
                        break
                    key=(host_id,task['id'])
                    if key in self.monitor.uncertain:
                        result=dict(outcome='blocked',reason='已有未确认或删除记录，不自动重复操作')
                    else:
                        try:
                            self.monitor.record(host_id,task['id'],'manual_started')
                            self.monitor.uncertain.add(key)
                            result=perform(host,command,task,self.monitor.data_dir,plan.get('progress_limit'))
                            event={'started':'manual_resumed','paused':'manual_paused','deleted':'manual_deleted_files' if COMMANDS[command][2] else 'manual_deleted_keep_files','skipped':'manual_skipped_changed'}[result['outcome']]
                            self.monitor.record(host_id,task['id'],event)
                            if result['outcome'] in ('paused','started','skipped'):
                                self.monitor.uncertain.discard(key)
                        except Exception:
                            result=dict(outcome='uncertain',reason='结果未确认，禁止自动重试；请到迅雷核对')
                            try:self.monitor.record(host_id,task['id'],'manual_uncertain')
                            except OSError:pass
                    self.monitor.trackers[host_id].forget(task['id'])
                    with self.lock:
                        self.jobs[job_id]['results'].append(dict(id=task['id'],name=task['name'],**result))
                    if result['outcome']=='uncertain':
                        break  # don't compound unknown outcomes across an entire batch
        finally:
            with self.lock:
                job=self.jobs[job_id]
                job['state']='done' if len(job['results'])==job['total'] and not any(r['outcome']=='uncertain' for r in job['results']) else 'stopped'
                job['phase']='finished'
                self.busy.discard(host_id)

    def _run_chunks(self, job_id, plan):
        host, command = plan['host'], plan['command']
        host_id = host['id']
        cancel = threading.Event()
        pending = []
        for task in plan['tasks']:
            if (host_id, task['id']) in self.monitor.uncertain:
                with self.lock:
                    self.jobs[job_id]['results'].append(dict(id=task['id'], name=task['name'], outcome='blocked'))
            else:
                pending.append(task)
        chunks = [pending[i:i+8] for i in range(0, len(pending), 8)]

        def run_chunk(chunk):
            if self.monitor.stop.is_set() or cancel.is_set():
                return
            # Write all intents before the single remote call. Unsent tasks in
            # an interrupted chunk remain protected until explicitly reviewed.
            try:
                for task in chunk:
                    self.monitor.record(host_id, task['id'], 'manual_started')
                    self.monitor.uncertain.add((host_id, task['id']))
                rows = perform_many(host, command, chunk, self.monitor.data_dir, plan.get('progress_limit'))
            except Exception:
                rows = [dict(id=t['id'], outcome='uncertain', reason='批次结果未确认，请核对目标机；不自动重试') for t in chunk]
            indexed = {t['id']: t for t in chunk}
            for result in rows:
                task = indexed[result['id']]
                outcome = result['outcome']
                event = {'started': 'manual_resumed', 'paused': 'manual_paused',
                         'deleted': 'manual_deleted_files' if COMMANDS[command][2] else 'manual_deleted_keep_files',
                         'skipped': 'manual_skipped_changed', 'uncertain': 'manual_uncertain'}[outcome]
                self.monitor.record(host_id, task['id'], event)
                if outcome in ('started', 'paused', 'skipped'):
                    self.monitor.uncertain.discard((host_id, task['id']))
                self.monitor.trackers[host_id].forget(task['id'])
                with self.lock:
                    self.jobs[job_id]['results'].append(dict(result, id=task['id'], name=task['name']))
                if outcome == 'uncertain':
                    cancel.set()

        # Start is reversible; two independent native connections can run at
        # once. Pause/delete retain ordered chunks and stop on uncertainty.
        if command == 'start_all':
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix='start-batch') as pool:
                for offset in range(0, len(chunks), 2):
                    if cancel.is_set() or self.monitor.stop.is_set():
                        break
                    for future in as_completed([pool.submit(run_chunk, c) for c in chunks[offset:offset+2]]):
                        future.result()
        else:
            for chunk in chunks:
                run_chunk(chunk)
                if cancel.is_set() or self.monitor.stop.is_set():
                    break

    def status(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise ValueError('操作记录不存在')
            job = self.jobs[job_id]
            return dict(job, processed=len(job['results']), results=[dict(row) for row in job['results'][-30:]])

    def snapshot(self):
        with self.lock:
            return [{k: v for k, v in job.items() if k != 'results'} | {'processed': len(job['results'])}
                    for job in self.jobs.values()]

    def join(self):
        for t in self.threads:
            t.join()
