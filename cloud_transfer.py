"""Tenant-scoped native Thunder cloud jobs, using the supplied CLI's engine.

All cloud calls execute on the user's Windows machine through its logged-in
Thunder SDK. No cloud cookies or tokens leave that machine.
"""
import base64
import hashlib
import json
import math
import re
import secrets
import threading
import time
from resource_limits import PLAN_SECONDS, remote_io
from pathlib import Path

from machine_settings import atomic_json
from thunder_api import ssh_client, read_live_tasks, read_task_archive

BUNDLE = Path(__file__).resolve().parent / 'remote/cloud'
FILES = ('engine.ps1', 'manual.ps1', 'ipc.ps1', 'worker.ps1', 'rules.json')
REVISION = hashlib.sha256(b''.join((BUNDLE / name).read_bytes() for name in FILES)).hexdigest()[:16]
COMMANDS = {'transfer': 'Transfer', 'recycle': 'Recycle', 'download': 'Download', 'sync': 'Sync'}


def options(body):
    limit = body.get('progress_limit', 20)
    wait = body.get('wait_minutes', 5)
    if type(limit) not in (int, float) or not math.isfinite(limit) or not 0 < limit <= 100:
        raise ValueError('转存进度门槛必须大于 0 且不超过 100%')
    if type(wait) is not int or not 0 <= wait <= 1440:
        raise ValueError('等待时间须为 0–1440 的整数分钟')
    output = body.get('output', '')
    folder = body.get('folder', '')
    cache_only = body.get('cache_only', False)
    if type(cache_only) is not bool or not isinstance(output, str) or not isinstance(folder, str):
        raise ValueError('云盘选项格式错误')
    if len(output) > 240 or any(ord(c) < 32 for c in output):
        raise ValueError('下载位置无效')
    if output:
        if not re.match(r'^(?:[A-Za-z]:\\|\\\\[^\\]+\\[^\\]+)', output):
            raise ValueError('请输入目标 Windows 的绝对路径，例如 D:\\下载')
        for part in re.split(r'\\', output[3:] if re.match(r'^[A-Za-z]:', output) else output[2:]):
            if part and (part in ('.', '..') or re.search(r'[:*?"<>|]', part) or part.endswith((' ', '.'))
                         or re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', part, re.I)):
                raise ValueError('下载路径包含不支持的名称')
    if folder and not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', folder):
        raise ValueError('云盘目录 ID 格式无效')
    return dict(progress_limit=limit, wait_minutes=wait, output=output, folder=folder, cache_only=cache_only)


def transferable(task, limit=20, recycle=False):
    if task.get('is_pan_task') or task.get('origin') == 'cloud_bridge_v1' or task.get('task_type') not in (1, 2, 5):
        return False
    if not task.get('save_path') or (not task.get('url') and task.get('task_type') == 1):
        return False
    if recycle:
        return task.get('view') == 'recycle' and task.get('recycle_time', 0) > 0
    return (not task.get('read_only') and task.get('view', 'incomplete') == 'incomplete'
            and not task.get('recycle_time', 0) and task.get('active') is True and task.get('status') == 5
            and type(task.get('speed')) is int and task['speed'] == 0 and type(task.get('size')) is int
            and type(task.get('received')) is int and task['size'] > 0
            and 0 <= task['received'] * 100 < task['size'] * limit)


def _ps_string(value):
    return "'" + value.replace("'", "''") + "'"


def _exec(client, script, timeout=180):
    encoded = base64.b64encode(script.encode('utf-16-le')).decode()
    command = 'powershell.exe -NoProfile -NonInteractive -EncodedCommand ' + encoded
    _, stdout, _ = client.exec_command(command, timeout=timeout)
    channel = stdout.channel
    chunks = []
    size = 0
    deadline = time.monotonic() + timeout
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError('远端调用超时，请核对请求状态，不要重复提交')
        if channel.recv_ready():
            block = channel.recv(65536)
            chunks.append(block)
            size += len(block)
        if channel.recv_stderr_ready():
            channel.recv_stderr(65536)  # SDK diagnostics must never reach the browser.
        if size > 8 * 1024 * 1024:
            raise ValueError('远端响应过大')
        if channel.exit_status_ready() and (channel.eof_received or channel.closed) and not channel.recv_ready() and not channel.recv_stderr_ready():
            break
        time.sleep(.01)
    if channel.recv_exit_status() != 0:
        raise RuntimeError('云盘模块执行失败，请核对目标机登录、权限及请求状态')
    for line in b''.join(chunks).decode('utf-8-sig').split('\n'):
        if line.startswith('THUNDER_WEB_JSON:'):
            return json.loads(line.split(':', 1)[1])
    raise RuntimeError('未收到云盘模块结果')


def _json_file(sftp, path, default=None):
    try:
        with sftp.open(path, 'rb') as stream:
            data = stream.read(8 * 1024 * 1024 + 1)
    except FileNotFoundError:
        return default
    if len(data) > 8 * 1024 * 1024:
        raise ValueError('转存状态文件过大')
    return json.loads(data.decode('utf-8-sig'))


class CloudRemote:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.layouts = {}

    def namespace(self, host):
        identity = str(self.data_dir.resolve()) + '\0' + host['id'] + '\0' + host['address'] + ':' + str(host.get('port', 22))
        return hashlib.sha256(identity.encode()).hexdigest()[:32]

    def layout(self, client, host):
        name = self.namespace(host)
        if name in self.layouts:
            return self.layouts[name]
        result = _exec(client, "[Console]::OutputEncoding=[Text.Encoding]::UTF8;$p=Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'ThunderWebUI\\" + name + "';Write-Output ('THUNDER_WEB_JSON:'+(@{path=$p}|ConvertTo-Json -Compress))")
        path = result.get('path', '')
        if not isinstance(path, str) or not re.match(r'^[A-Za-z]:\\', path) or not path.endswith('ThunderWebUI\\' + name):
            raise ValueError('远端模块路径无效')
        self.layouts[name] = path.replace('\\', '/')
        return self.layouts[name]

    def deploy(self, client, sftp, root):
        code = root + '/code-' + REVISION
        _exec(client, "$ErrorActionPreference='Stop';New-Item -ItemType Directory -Force -Path " + _ps_string(code) + "|Out-Null;Write-Output 'THUNDER_WEB_JSON:{}'")
        # The versioned directory is immutable once the manifest is published.
        manifest = _json_file(sftp, code + '/manifest.json', {})
        if manifest.get('revision') != REVISION:
            for name in FILES:
                with sftp.open(code + '/' + name, 'wb') as out:
                    out.write((BUNDLE / name).read_bytes())
            with sftp.open(code + '/manifest.json', 'w') as out:
                out.write(json.dumps({'revision': REVISION}))
        return code

    @remote_io
    def install(self, host):
        client = ssh_client(host, self.data_dir)
        try:
            root = self.layout(client, host)
            with client.open_sftp() as sftp:
                self.deploy(client, sftp, root)
            return {'installed': True, 'revision': REVISION}
        finally:
            client.close()

    @remote_io
    def status(self, host, job_ids=()):
        client = ssh_client(host, self.data_dir)
        try:
            root = self.layout(client, host)
            with client.open_sftp() as sftp:
                sftp.get_channel().settimeout(20)
                state = _json_file(sftp, root + '/运行记录/状态模块-转存记录-v1.json', {})
                try:
                    names = sftp.listdir(root + '/运行记录')
                except FileNotFoundError:
                    names = []
                requests = []
                for name in names:
                    if re.fullmatch(r'请求模块-[a-f0-9]{32}-v1.json', name):
                        requests.append(_json_file(sftp, root + '/运行记录/' + name, {}))
                jobs = {key: _json_file(sftp, root + '/web-' + key + '.status.json') for key in job_ids}
            return {'state': state, 'requests': requests, 'workers': jobs}
        finally:
            client.close()

    @remote_io
    def list_cloud(self, host, opts):
        client = ssh_client(host, self.data_dir)
        try:
            root = self.layout(client, host)
            with client.open_sftp() as sftp:
                code = self.deploy(client, sftp, root)
            script = r"""
$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.Encoding]::UTF8
. __CODE__ -WorkDir __ROOT__ -LibraryOnly
Read-State
# Default scope is this workspace's dedicated folder, never the entire account.
$folder=__FOLDER__
if(-not $folder){$folder=[string]$script:State.rootFolderId;if(-not $folder){Write-Output 'THUNDER_WEB_JSON:{"resources":[],"scope":"当前工作空间尚无转存目录"}';return}}
$connection=$null
try {
 $connection=Open-ThunderConnection
 $options=@{command='ListCloud';folder=$folder;dry=$true}
 $script:ManualOptions=$options
 $r=Invoke-ManualCommand $connection $options
 Write-Output ('THUNDER_WEB_JSON:'+($r|ConvertTo-Json -Depth 30 -Compress))
}finally{Close-ThunderConnection $connection}
""".replace('__CODE__', _ps_string(code + '/manual.ps1')).replace('__ROOT__', _ps_string(root)).replace('__FOLDER__', _ps_string(opts['folder']))
            return _exec(client, script, 300)
        finally:
            client.close()

    @remote_io
    def launch(self, host, job):
        client = ssh_client(host, self.data_dir)
        try:
            root = self.layout(client, host)
            key = job['id']
            scheduled = 'ThunderWebUI-' + self.namespace(host)[:12] + '-' + key
            with client.open_sftp() as sftp:
                code = self.deploy(client, sftp, root)
                payload = dict(id=key, command=COMMANDS[job['command']], options=job['options'], expected=job.get('tasks', []), cleanupRecycle=job.get('cleanup_recycle', False),
                               units=job.get('units', []), workDir=root, statusPath=root + '/web-' + key + '.status.json', scheduledTask=scheduled)
                input_path = root + '/web-' + key + '.input.json'
                with sftp.open(input_path, 'wb') as stream:
                    stream.write(json.dumps(payload, ensure_ascii=False).encode('utf-8-sig'))
            body = '& ' + _ps_string(code + '/worker.ps1') + ' -InputPath ' + _ps_string(input_path)
            encoded = base64.b64encode(body.encode('utf-16-le')).decode()
            script = r"""
$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.Encoding]::UTF8
$name=__NAME__
if(Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue){throw '任务已登记，不重复执行'}
$action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument __ARG__
$principal=New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Highest
$settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $name -Action $action -Principal $principal -Settings $settings -Force|Out-Null
Start-ScheduledTask -TaskName $name
Write-Output 'THUNDER_WEB_JSON:{"launched":true}'
""".replace('__NAME__', _ps_string(scheduled)).replace('__ARG__', _ps_string('-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand ' + encoded))
            return _exec(client, script)
        finally:
            client.close()


def public_status(raw):
    """Only allow fields the product displays; never return native SDK dumps."""
    source = raw.get('state') or {}
    jobs = []
    for item in (source.get('jobs') or {}).values():
        downloads = []
        for row in (item.get('downloads') or {}).values():
            downloads.append({key: row.get(key) for key in ('fileId', 'name', 'taskId', 'stage', 'savePath', 'size', 'startedAt', 'movedAt')})
            downloads[-1]['needs_review'] = bool(row.get('error'))
        jobs.append(dict(id=item.get('id'), name=item.get('name'), stage=item.get('stage'), source=item.get('source'),
                         folderId=item.get('folderId'), originalDeleted=item.get('originalDeleted', False),
                         recycle_cleanup=item.get('recycleCleanup') if item.get('cleanupRecycle') is True else None,
                         recycle_cleanup_review=bool(item.get('recycleCleanupError')),
                         needs_review=bool(item.get('error')), lastCheckReason='需人工核对' if item.get('error') else item.get('lastCheckReason', ''),
                         downloads=downloads))
    requests = []
    for row in raw.get('requests', []):
        value = {key: row.get(key) for key in ('id', 'stage', 'createdAt', 'notBefore', 'checkedAt', 'jobIds', 'cacheOnly', 'waitMinutes', 'cleanupRecycle', 'cleanupStage', 'cleanupPending', 'cleanupCleaned', 'cleanupReview', 'cleanupNextCheck')}
        value['failed'] = len(row.get('errors') or [])
        value['results'] = [dict(id=r.get('id'), ready=r.get('ready'), reason='缓存未完整或需要核对' if not r.get('ready') else '整项缓存完成') for r in row.get('results', [])]
        requests.append(value)
    return dict(jobs=sorted(jobs, key=lambda j: str(j['id'])), requests=sorted(requests, key=lambda r: r.get('createdAt') or 0, reverse=True),
                root_folder=source.get('rootFolderId', ''), updated_at=source.get('updatedAt', ''))


class CloudTransfers:
    def __init__(self, monitor):
        self.m = monitor
        self.remote = CloudRemote(monitor.data_dir)
        self.lock = threading.RLock()
        self.plans = {}
        self.jobs = {}
        self.host_states = {}
        self.busy = set()
        self.polling = set()
        self.threads = []
        self.journal = monitor.data_dir / '.runtime/cloud-jobs.json'
        if self.journal.exists():
            self.jobs = json.loads(self.journal.read_text())
            for job in self.jobs.values():
                if job['state'] in ('dispatching', 'running'):
                    job['state'] = 'recovering'
                    self.busy.add(job['host'])

    def save(self):
        atomic_json(self.journal, self.jobs)

    def host(self, host_id):
        host = self.m.actions.host(host_id)
        if not host:
            raise ValueError('请选择本工作空间的机器')
        return host

    def protected(self, host_id, task_id):
        with self.lock:
            # Persistently keep transferred originals out of legacy auto cleanup.
            return any(j['host'] == host_id and any(t['id'] == task_id for t in j.get('tasks', [])) for j in self.jobs.values())

    def is_busy(self, host_id):
        with self.lock:
            return host_id in self.busy

    def prepare(self, body):
        host = self.host(body.get('host'))
        command = body.get('command')
        if command not in COMMANDS:
            raise ValueError('请选择转存、回收站转存或云盘下载')
        opts = options(body)
        ids = body.get('task_ids')
        if ids is not None and (not isinstance(ids, list) or len(ids) > 10000 or not ids or any(type(i) is not int or i <= 0 for i in ids)):
            raise ValueError('任务 ID 列表无效')
        if command == 'recycle' and ids is None and body.get('all') is not True:
            raise ValueError('请指定回收站任务，或选择全部可转存项目')
        with self.lock:
            if self.m.stop.is_set() or host['id'] in self.busy or host['id'] in self.m.actions.busy:
                raise ValueError('服务关闭中或该机器正在执行云盘操作')
            self.plans = {k: v for k, v in self.plans.items() if v['expires'] > time.monotonic()}
            if len(self.plans) >= 30:
                raise ValueError('待确认操作过多，请稍后重试')
        units = []
        # These are read-only previews. Do not queue them behind full live and
        # archive polling; the detached worker revalidates before any mutation.
        if command == 'sync':
            raw = self.remote.status(host)
            units = [dict(id=j.get('id'),name=j.get('name'),remaining=0) for j in (raw.get('state', {}).get('jobs') or {}).values()
                     if any(r.get('stage') in ('ready','created','started','alreadyLocal') for r in j.get('downloads', {}).values())]
            tasks = []
        elif command == 'download':
            if not opts['output']:
                raise ValueError('云盘下载需填写目标 Windows 保存路径')
            result = self.remote.list_cloud(host, opts)
            units = [u for u in result.get('resources', []) if u.get('ready') and u.get('remaining', 0) > 0]
            tasks = []
        else:
            # Reuse a recent live sample; mutations always revalidate on Windows.
            with self.m.lock:
                cached = self.m.hosts.get(host['id'], {})
                fresh = cached.get('state') == '在线' and time.time() - cached.get('checked_at', 0) < 60
                rows = list(cached.get('tasks', [])) if fresh and command == 'transfer' else None
            if rows is None:
                rows = read_task_archive(host, 'recycle', self.m.data_dir) if command == 'recycle' else read_live_tasks(host, self.m.data_dir, active_only=True)
            status = self.remote.status(host)
            tracked = set((status.get('state') or {}).get('jobs', {}))
            tasks = [t for t in rows if transferable(t, opts['progress_limit'], command == 'recycle')
                     and str(t['id']) not in tracked and not self.protected(host['id'], t['id'])
                     and (ids is None or t['id'] in ids)]
        token = secrets.token_urlsafe(32)
        plan = dict(host=host['id'], host_revision=host.get('revision'), command=command, cleanup_recycle=command == 'recycle' and body.get('cleanup_recycle') is True, options=opts, tasks=tasks, units=units, expires=time.monotonic() + PLAN_SECONDS)
        with self.lock:
            self.plans[token] = plan
        return dict(plan=token, host=host['id'], command=command, cleanup_recycle=plan['cleanup_recycle'], options=opts, count=len(units) if command in ('download','sync') else len(tasks),
                    tasks=[{key: t.get(key) for key in ('id', 'name', 'save_path', 'size', 'received')} for t in tasks],
                    resources=[{key: u.get(key) for key in ('id', 'name', 'remaining', 'folderId')} for u in units], expires_seconds=PLAN_SECONDS)

    def execute(self, token):
        with self.lock:
            plan = self.plans.get(token)
            if not plan or plan['expires'] <= time.monotonic():
                raise ValueError('云盘预览已过期，请重新预览')
            host=self.host(plan['host'])
            if host.get('revision')!=plan.get('host_revision'):
                raise ValueError('机器配置已变化，请重新预览')
            if self.m.stop.is_set() or plan['host'] in self.busy or plan['host'] in self.m.actions.busy:
                raise ValueError('服务关闭中或该机器正在执行操作')
            if not plan['tasks'] and not plan['units']:
                raise ValueError('没有符合条件的项目')
            job_id = secrets.token_hex(16)
            job = {key: value for key, value in plan.items() if key != 'expires'}
            job.update(id=job_id, state='dispatching', phase='waiting_for_machine', processed=0,
                       total=len(plan['tasks']) or len(plan['units']), created_at=time.time())
            self.jobs[job_id] = job
            self.save()  # publish intent before the first remote mutation
            self.plans.pop(token)
            self.busy.add(plan['host'])
            thread = threading.Thread(target=self._launch, args=(job_id,), daemon=True)
            self.threads = [t for t in self.threads if t.is_alive()] + [thread]
            thread.start()
        return {'job': job_id}

    def _launch(self, key):
        job = self.jobs[key]
        try:
            host = self.host(job['host'])
            with self.m.operation_locks[host['id']]:
                with self.lock:
                    job['phase'] = 'launching'
                    self.save()
                result = self.remote.launch(host, job)
                if result.get('launched') is not True:
                    raise RuntimeError('未确认启动')
            with self.lock:
                if job['state'] == 'dispatching':
                    job['state'] = 'running'
                    job['phase'] = 'waiting_for_worker'
                    self.save()
        except Exception:
            with self.lock:
                job['state'] = 'uncertain'
                job['error'] = '远端启动结果未确认，请查看请求状态；不会自动重复提交'
                self.save()
                self.busy.discard(job['host'])
        self.refresh(job['host'], force=True)

    def refresh(self, host_id, force=False):
        with self.lock:
            previous = self.host_states.get(host_id, {})
            interval = 5 if host_id in self.busy else 15
            if host_id in self.polling or (not force and time.time() - previous.get('attempted_at', 0) < interval):
                return
            self.polling.add(host_id)
        host=None
        try:
            host=self.host(host_id)
            with self.lock:
                ids = [j['id'] for j in self.jobs.values() if j['host'] == host_id]
            raw = self.remote.status(host, ids)
            public = public_status(raw)
            with self.lock:
                if self.m.actions.host(host_id) is not host:return
                for key, worker in raw.get('workers', {}).items():
                    job = self.jobs[key]
                    if worker and worker.get('id') == key:
                        job['state'] = worker['state'] if worker.get('state') in ('running', 'done', 'error') else 'uncertain'
                        progress = worker.get('progress') or {}
                        job['phase'] = progress.get('phase', '')
                        job['processed'] = progress.get('processed', 0)
                        job['total'] = progress.get('total', len(job.get('tasks', [])))
                        job['current_task'] = progress.get('current', '')
                        job['updated_at'] = worker.get('updatedAt')
                        result = worker.get('result') or {}
                        request = result.get('request') or {}
                        job['request_id'] = request.get('id')
                        job['failed'] = result.get('failed', 0)
                        if job['state'] == 'error':
                            job['error'] = '远端执行失败，请核对请求及转存状态，不要重复提交'
                    elif job['state'] in ('running', 'dispatching', 'recovering') and time.time() - job['created_at'] > 180:
                        job['state'] = 'uncertain'
                        job['error'] = '未找到远端执行状态，请核对 Windows 登录及计划任务；不自动重发'
                self.busy = {j['host'] for j in self.jobs.values() if j['state'] in ('running', 'dispatching', 'recovering')}
                self.host_states[host_id] = dict(public, state='ready', checked_at=time.time(), attempted_at=time.time())
                self.save()
        except Exception:
            with self.lock:
                if host is not None and self.m.actions.host(host_id) is host:
                    self.host_states[host_id] = dict(previous, state='error', attempted_at=time.time(), error='云盘状态读取失败，保留上次结果')
        finally:
            with self.lock:
                self.polling.discard(host_id)

    def refresh_async(self, host_id, force=False):
        # Called by the normal monitor scheduler; cloud status errors must not
        # affect live task polling and download speed samples.
        with self.lock:
            if host_id not in self.host_states and not any(j['host'] == host_id for j in self.jobs.values()):
                return
            interval = 5 if host_id in self.busy else 15
            if self.m.stop.is_set() or host_id in self.polling or time.time() - self.host_states.get(host_id, {}).get('attempted_at', 0) < (0 if force else interval):
                return
            self.host_states.setdefault(host_id, {})['attempted_at'] = time.time()
            thread = threading.Thread(target=self.refresh, args=(host_id, True), daemon=True)
            self.threads = [t for t in self.threads if t.is_alive()] + [thread]
            thread.start()

    def inspect(self, body):
        host = self.host(body.get('host'))
        action = body.get('action', 'status')
        if action == 'install':
            with self.m.operation_locks[host['id']]:
                return self.remote.install(host)
        if action == 'list':
            opts = options(body)
            result = self.remote.list_cloud(host, opts)
            return dict(scope=result.get('scope'), resources=[{key: row.get(key) for key in ('id', 'name', 'folderId', 'ready', 'remaining')} for row in result.get('resources', [])])
        if action != 'status':
            raise ValueError('云盘查看命令无效')
        self.refresh(host['id'], force=True)
        return self.snapshot()

    def snapshot(self):
        with self.lock:
            jobs = [{key: j.get(key) for key in ('id', 'host', 'command', 'state', 'phase', 'processed', 'total', 'current_task', 'updated_at', 'created_at', 'request_id', 'failed', 'error')} for j in self.jobs.values()]
            return json.loads(json.dumps(dict(hosts={k: {key: value for key, value in v.items() if key != 'attempted_at'} for k, v in self.host_states.items()}, jobs=jobs)))

    def join(self):
        for thread in list(self.threads):
            thread.join()
