"""Thunder IPC over SSH; no remote installation and no file-delete option."""
import base64
import gzip
import json
import time
from pathlib import Path
import paramiko
from resource_limits import remote_io
from paths import DATA

ROOT = Path(__file__).resolve().parent


def ssh_client(host, data_dir=None):
    root = Path(data_dir).resolve() if data_dir is not None else DATA
    credential_path = (root / host['credentials_file']).resolve()
    if data_dir is not None:
        credential_path.relative_to(root)
    credentials = credential_path.read_text(encoding='utf-8-sig').splitlines()
    if len(credentials) < 2:
        raise ValueError('凭据需要两行')
    for attempt in range(2):
        client = paramiko.SSHClient()
        client.load_host_keys(str(root / '.runtime/known_hosts'))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(host['address'], port=host.get('port',22), username=credentials[0], password=credentials[1],
                           look_for_keys=False, allow_agent=False, timeout=8, banner_timeout=8, auth_timeout=8)
            client.get_transport().default_channel_timeout = 20
            return client
        except (ConnectionResetError, EOFError):
            # Retry only connection setup, before any remote command exists.
            # Never replay a mutation or retry invalid credentials/host keys.
            client.close()
            if attempt:
                raise
            time.sleep(.3)
        except Exception:
            client.close()
            raise


@remote_io
def remote(host, action, data_dir=None):
    script = (ROOT / 'remote/thunder_ipc.ps1').read_text().replace('__ACTION__', action)
    script = '\n'.join(line.strip() for line in script.splitlines() if line.strip())
    packed = base64.b64encode(gzip.compress(script.encode())).decode()
    bootstrap = ("$g=[IO.Compression.GZipStream]::new([IO.MemoryStream]::new([Convert]::FromBase64String('" + packed +
                 "')),[IO.Compression.CompressionMode]::Decompress); & ([ScriptBlock]::Create(([IO.StreamReader]::new($g)).ReadToEnd()))")
    command = 'powershell.exe -NoProfile -NonInteractive -Command "' + bootstrap + '"'
    if len(command) > 8000:
        raise ValueError('命令超出 Windows 长度限制')
    client = ssh_client(host, data_dir)
    try:
        _, stdout, stderr = client.exec_command(command, timeout=45)
        channel = stdout.channel
        chunks, errors, size = [], [], 0
        deadline = time.monotonic() + 45
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError('远程查询超时')
            if channel.recv_ready():
                b = channel.recv(65536); chunks.append(b); size += len(b)
            if channel.recv_stderr_ready():
                b = channel.recv_stderr(65536); size += len(b)
            if size > 8 * 1024 * 1024:
                raise ValueError('响应过大')
            # An exit status may arrive before the final stdout packets. Wait
            # for EOF/close as well, or large task lists can be cut mid-JSON.
            if (channel.exit_status_ready() and (channel.eof_received or channel.closed)
                    and not channel.recv_ready() and not channel.recv_stderr_ready()):
                break
            time.sleep(0.01)
        if channel.recv_exit_status() != 0:
            raise RuntimeError('远程接口执行失败')
        for line in b''.join(chunks).decode('utf-8-sig').split('\n'):
            if line.startswith('THUNDER_CLI_ERROR:'):
                raise RuntimeError('迅雷接口：' + str(json.loads(line.split(':',1)[1]).get('message','调用失败'))[:300])
            if line.startswith('THUNDER_CLI_JSON:'):
                return json.loads(line.split(':',1)[1])
        raise RuntimeError('未收到迅雷接口结果')
    finally:
        client.close()


LIST = r"""
$activeReply = Invoke-ThunderApi $connection 'CategoryManagerCategoryGetDownloadingTaskIds' @(-1)
$allReply = Invoke-ThunderApi $connection 'CategoryManagerCategoryViewGetTasks' @(-1,1)
if ($null -eq $activeReply.data -or $null -eq $allReply.data) { throw '缺少列表' }
$active=@($activeReply.data); $ids=@($allReply.data)
if($ids.Count -gt 10000){throw '任务过多'}
$rows=[Collections.Generic.List[object]]::new()
foreach($task in @(Get-ThunderRows $connection $ids)) {
 $id=$task.taskId
 if($null -eq $task -or $task.taskId -ne $id -or $task.recycleTime -gt 0){continue}
 $row=Convert-ThunderTask $task
 $row | Add-Member -NotePropertyName active -NotePropertyValue ($id -in $active)
 $rows.Add($row)
}
Write-CliResult @{tasks=$rows.ToArray(); activeCount=$active.Count}
"""


def number(value, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError('无效数值')
    return value


def read_live_tasks(host, data_dir=None, *, active_only=False):
    script = LIST
    if active_only:
        # A transfer preview only needs currently running tasks, not hundreds
        # of queued tasks. Execution still validates the confirmed identities.
        script = LIST.replace("$allReply = Invoke-ThunderApi $connection 'CategoryManagerCategoryViewGetTasks' @(-1,1)", '')
        script = script.replace('$null -eq $activeReply.data -or $null -eq $allReply.data', '$null -eq $activeReply.data')
        script = script.replace('$ids=@($allReply.data)', '$ids=@($activeReply.data)')
    data = remote(host, script, data_dir)
    tasks = data.get('tasks')
    if not isinstance(tasks,list) or len(tasks)>10000:
        raise ValueError('无效任务列表')
    result=[]
    for t in tasks:
        if not isinstance(t.get('active'),bool):
            raise ValueError('缺少实时下载列表成员标记')
        result.append(dict(id=number(t['taskId']), status=number(t['taskStatus']),
                           received=number(t['downloadSize']), size=number(t['fileSize']),
                           speed=number(t.get('downloadSpeed'),True), active=t['active'],
                           name=str(t.get('taskName') or ''), save_path=str(t.get('savePath') or ''), completed=0,
                           view='incomplete', read_only=False, recycle_time=number(t.get('recycleTime') or 0),
                           url=str(t.get('url') or ''), task_type=t.get('taskType'),
                           is_pan_task=t.get('isPanTask') is True, origin=str(t.get('origin') or ''),
                           created_at=number(t.get('createTime') or 0), completed_at=number(t.get('completionTime') or 0)))
    if len({t['id'] for t in result}) != len(result):
        raise ValueError('任务 ID 重复')
    return result


ARCHIVE_LIST = r"""
$reply = Invoke-ThunderApi $connection 'CategoryManagerCategoryViewGetTasks' @(-1,__VIEW__)
if($null -eq $reply.data){throw '缺少归档任务列表'}
$ids=@($reply.data)
if($ids.Count -gt 10000){throw '归档任务过多'}
$rows=[Collections.Generic.List[object]]::new()
foreach($task in @(Get-ThunderRows $connection $ids)) {
 $id=$task.taskId
 if($null -eq $task -or $task.taskId -ne $id){continue}
 if(__VIEW__ -eq 2 -and $task.recycleTime -gt 0){continue}
 $rows.Add((Convert-ThunderTask $task))
}
Write-CliResult @{tasks=$rows.ToArray()}
"""


def read_task_archive(host, view, data_dir=None):
    if view not in ('completed', 'recycle'):
        raise ValueError('归档视图无效')
    data = remote(host, ARCHIVE_LIST.replace('__VIEW__', '2' if view == 'completed' else '3'), data_dir)
    tasks = data.get('tasks')
    if not isinstance(tasks, list) or len(tasks) > 10000:
        raise ValueError('归档任务列表无效')
    result = []
    for task in tasks:
        result.append(dict(
            id=number(task['taskId']), name=str(task.get('taskName') or ''),
            status=number(task['taskStatus']), task_type=task.get('taskType'),
            is_pan_task=task.get('isPanTask') is True, origin=str(task.get('origin') or ''),
            size=number(task['fileSize']), received=number(task['downloadSize']),
            save_path=str(task.get('savePath') or ''), url=str(task.get('url') or ''),
            recycle_time=number(task.get('recycleTime') or 0),
            created_at=number(task.get('createTime') or 0),
            completed_at=number(task.get('completionTime') or 0),
            view=view, read_only=True, active=False, speed=None,
            unchanged_seconds=0, delete_eligible=False, needs_review=False,
            protection_reason='回收站仅供查看' if view == 'recycle' else '已完成任务仅供查看'))
    if len({t['id'] for t in result}) != len(result):
        raise ValueError('归档任务 ID 重复')
    return result


DELETE = r"""
$id=__ID__
$expectedName=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__NAME__'))
$expectedPath=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PATH__'))
$expectedBytes=__BYTES__
function Test-Current($t,$ids) {
 return ($null -ne $t -and $t.taskId -eq $id -and $id -in $ids -and $t.recycleTime -le 0 -and
 $t.taskStatus -eq 5 -and $null -ne $t.downloadSpeed -and $t.downloadSpeed -eq 0 -and
 $t.downloadSize -eq $expectedBytes -and $t.taskName -ceq $expectedName -and $t.savePath -ceq $expectedPath)
}
for($check=0;$check -lt 2;$check++) {
 $ids=@((Invoke-ThunderApi $connection 'CategoryManagerCategoryGetDownloadingTaskIds' @(-1)).data)
 $t=(Invoke-ThunderApi $connection 'TaskManagerTaskGetTaskBase' @($id)).data
 if(!(Test-Current $t $ids)){Write-CliResult @{deleted=$false;reason='changed'};return}
 if($check -eq 0){Start-Sleep -Milliseconds 250}
}
$result=(Invoke-ThunderApi $connection 'TaskManagerTaskDeleteTask' @($id,$false)).data
if($null -eq $result -or $result -ne 0){throw '移除任务失败'}
$after=(Invoke-ThunderApi $connection 'TaskManagerTaskGetTaskBase' @($id)).data
if($after.taskId -eq $id){throw '移除结果不确定'}
Write-CliResult @{deleted=$true;filesDeleted=$false;taskId=$id}
"""


def remove_keep_files(host, task, data_dir=None):
    action=DELETE.replace('__ID__',str(number(task['id']))).replace('__BYTES__',str(number(task['received'])))
    for key,field in [('__NAME__','name'),('__PATH__','save_path')]:
        action=action.replace(key,base64.b64encode(task[field].encode()).decode())
    return remote(host,action,data_dir)
