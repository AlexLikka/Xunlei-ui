"""Snapshot-bound manual commands, matching the supplied Thunder CLI."""
import base64
import json
import math
import secrets
import threading
import time
from thunder_api import read_live_tasks, remote

COMMANDS = {
    'pause_zero': ('暂停当前零速任务', 'pause', False, False, True),
    'del': ('删除任务，保留文件', 'delete', False, False, False),
    'delf': ('删除任务及本地文件', 'delete', True, False, False),
    'del_zero': ('删除当前零速任务，保留文件', 'delete', False, False, True),
    'delf_zero': ('删除当前零速任务及文件', 'delete', True, False, True),
    'del_zero20': ('删除零速且进度小于 20% 的任务，保留文件', 'delete', False, True, True),
    'delf_zero20': ('删除零速且进度小于 20% 的任务及文件', 'delete', True, True, True),
}


def candidate(t, under20=False):
    zero = t.get('active') is True and t.get('status') == 5 and type(t.get('speed')) is int and t['speed'] == 0
    if not zero:
        return False
    return not under20 or (type(t.get('size')) is int and type(t.get('received')) is int
                           and t['size'] > 0 and 0 <= t['received'] * 5 < t['size'])


ACTION = r"""
$p=([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__')) | ConvertFrom-Json)
$id=[long]$p.id
function Test-ManualCurrent($t) {
 if($null -eq $t -or $t.taskId -ne $id -or $t.taskName -cne $p.name -or $t.savePath -cne $p.path){return $false}
 if($p.bulk){
  $ids=@((Invoke-ThunderApi $connection 'CategoryManagerCategoryGetDownloadingTaskIds' @(-1)).data)
  if($id -notin $ids -or $t.taskStatus -ne 5 -or $null -eq $t.downloadSpeed -or $t.downloadSpeed -ne 0){return $false}
  if($p.under20 -and ($null -eq $t.fileSize -or $null -eq $t.downloadSize -or $t.fileSize -le 0 -or $t.downloadSize -lt 0 -or ([decimal]$t.downloadSize * 5) -ge [decimal]$t.fileSize)){return $false}
 }
 return $true
}
$t=(Invoke-ThunderApi $connection 'TaskManagerTaskGetTaskBase' @($id)).data
if(!(Test-ManualCurrent $t)){Write-CliResult @{outcome='skipped';reason='changed'};return}
if($p.mode -eq 'pause'){
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


def perform(host, command, task, data_dir=None):
    _, mode, files, under20, bulk = COMMANDS[command]
    payload = dict(id=task['id'], name=task['name'], path=task['save_path'], mode=mode, files=files, under20=under20, bulk=bulk)
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
    result = remote(host, ACTION.replace('__PAYLOAD__', encoded), data_dir)
    if result.get('outcome') not in ('paused', 'deleted', 'skipped'):
        raise RuntimeError('操作结果无法确认')
    if result['outcome'] == 'deleted' and (mode != 'delete' or result.get('filesRequested') is not files):
        raise RuntimeError('删除结果不匹配')
    if result['outcome'] == 'paused' and mode != 'pause':
        raise RuntimeError('暂停结果不匹配')
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

    def prepare(self, host_id, command, task_id=None):
        if self.monitor.stop.is_set():
            raise ValueError('服务正在关闭')
        host = self.host(host_id)
        if host is None or command not in COMMANDS:
            raise ValueError('主机或命令无效')
        with self.lock:
            if host_id in self.busy:
                raise ValueError('该主机正在执行操作，请等待完成')
            self.plans = {k:v for k,v in self.plans.items() if time.monotonic() < v['expires']}
            if len(self.plans) >= 30:
                raise ValueError('待确认操作过多，请等待过期')
        _, _, files, under20, bulk = COMMANDS[command]
        if not bulk and (type(task_id) is not int or task_id < 0):
            raise ValueError('请指定任务 ID')
        with self.monitor.operation_locks[host_id]:
            rows = read_live_tasks(host, self.monitor.data_dir)
        tasks = [t for t in rows if candidate(t,under20)] if bulk else [t for t in rows if t['id'] == task_id]
        if not bulk and not tasks:
            raise ValueError('任务不存在或已完成，请刷新列表')
        public = dict(host=host_id, command=command, label=COMMANDS[command][0], files=files, count=len(tasks),
                      tasks=[dict(id=t['id'], name=t['name'], path=t['save_path'], size=t['size'], received=t['received']) for t in tasks],
                      expires_seconds=120)
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.plans[token] = dict(host=host, command=command, tasks=tasks, expires=time.monotonic()+120)
        return {**public, 'plan':token}

    def execute(self, token, confirmation):
        with self.lock:
            plan = self.plans.get(token)
            if not plan or time.monotonic() >= plan['expires']:
                raise ValueError('预览已过期，请重新预览')
            host_id = plan['host']['id']
            if self.monitor.stop.is_set() or host_id in self.busy:
                raise ValueError('服务正在关闭或该主机正在执行操作')
            if COMMANDS[plan['command']][2] and confirmation != '删除文件':
                raise ValueError('删除本地文件需要输入“删除文件”确认')
            self.plans.pop(token)  # single use, also on failure
            self.busy.add(host_id)
            job_id = secrets.token_hex(12)
            self.jobs[job_id] = dict(id=job_id,host=host_id,command=plan['command'],state='running',total=len(plan['tasks']),results=[])
            # Keep only recent completed jobs.
            old = [k for k,v in self.jobs.items() if v['state'] != 'running']
            for k in old[:-49]:
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
                            result=perform(host,command,task,self.monitor.data_dir)
                            event={'paused':'manual_paused','deleted':'manual_deleted_files' if COMMANDS[command][2] else 'manual_deleted_keep_files','skipped':'manual_skipped_changed'}[result['outcome']]
                            self.monitor.record(host_id,task['id'],event)
                            if result['outcome'] in ('paused','skipped'):
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
                if not self.monitor.stop.is_set():
                    self.monitor._poll_host(host, allow_auto=False)
        finally:
            with self.lock:
                job=self.jobs[job_id]
                job['state']='done' if len(job['results'])==job['total'] else 'stopped'
                self.busy.discard(host_id)

    def status(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise ValueError('操作记录不存在')
            return json.loads(json.dumps(self.jobs[job_id]))

    def join(self):
        for t in self.threads:
            t.join()
