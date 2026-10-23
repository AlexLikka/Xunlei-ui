"""Thunder IPC over SSH; no remote installation and no file-delete option."""
import base64
import gzip
import json
import time
from pathlib import Path
import paramiko

ROOT = Path(__file__).resolve().parent


def remote(host, action):
    credentials = (ROOT / host['credentials_file']).read_text(encoding='utf-8-sig').splitlines()
    if len(credentials) < 2:
        raise ValueError('凭据需要两行')
    script = (ROOT / 'remote/thunder_ipc.ps1').read_text().replace('__ACTION__', action)
    script = '\n'.join(line.strip() for line in script.splitlines() if line.strip())
    packed = base64.b64encode(gzip.compress(script.encode())).decode()
    bootstrap = ("$g=[IO.Compression.GZipStream]::new([IO.MemoryStream]::new([Convert]::FromBase64String('" + packed +
                 "')),[IO.Compression.CompressionMode]::Decompress); & ([ScriptBlock]::Create(([IO.StreamReader]::new($g)).ReadToEnd()))")
    command = 'powershell.exe -NoProfile -NonInteractive -Command "' + bootstrap + '"'
    if len(command) > 8000:
        raise ValueError('命令超出 Windows 长度限制')
    client = paramiko.SSHClient()
    client.load_host_keys(str(ROOT / '.runtime/known_hosts'))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(host['address'], port=host.get('port',22), username=credentials[0], password=credentials[1],
                       look_for_keys=False, allow_agent=False, timeout=8, banner_timeout=8, auth_timeout=8)
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
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            time.sleep(0.01)
        if channel.recv_exit_status() != 0:
            raise RuntimeError('远程接口执行失败')
        for line in b''.join(chunks).decode('utf-8-sig').splitlines():
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
foreach($id in $ids) {
 $task=(Invoke-ThunderApi $connection 'TaskManagerTaskGetTaskBase' @($id)).data
 if($null -eq $task -or $task.taskId -ne $id){continue}
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


def read_live_tasks(host):
    data = remote(host, LIST)
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
                           name=str(t.get('taskName') or ''), save_path=str(t.get('savePath') or ''), completed=0))
    if len({t['id'] for t in result}) != len(result):
        raise ValueError('任务 ID 重复')
    return result


DELETE = r"""
$id=__ID__
$expectedName=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__NAME__'))
$expectedPath=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PATH__'))
$expectedBytes=__BYTES__
function Test-Current($t,$ids) {
 return ($null -ne $t -and $t.taskId -eq $id -and $id -in $ids -and
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


def remove_keep_files(host, task):
    action=DELETE.replace('__ID__',str(number(task['id']))).replace('__BYTES__',str(number(task['received'])))
    for key,field in [('__NAME__','name'),('__PATH__','save_path')]:
        action=action.replace(key,base64.b64encode(task[field].encode()).decode())
    return remote(host,action)
