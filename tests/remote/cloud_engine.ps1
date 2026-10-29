[CmdletBinding()]
param([string]$ModuleDir=$PSScriptRoot)
$ErrorActionPreference='Stop'
$testDir=Join-Path $env:TEMP ('迅雷转存测试-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testDir|Out-Null
try {
    . (Join-Path $ModuleDir 'engine.ps1') -LibraryOnly -WorkDir $testDir
    $script:passed=@()
    function Assert-Test([bool]$Condition,[string]$Name){if(-not $Condition){throw "验证失败：$Name"};$script:passed+=,$Name}
    function Save-State {}
    function Write-Event($Event,$Details) {}
    $script:State=@{ownerId='owner';jobs=@{};observations=@{}}
    $script:createCalls=0;$script:moveCalls=0;$script:lookup=0;$script:currentParent='source'
    $script:currentTask=@{taskId=42;taskName='测试.mkv';isPanTask=$true;savePath='D:\测试';taskStatus=3;downloadSize=0;downloadSpeed=0}
    function Get-LocalTask($Connection,[long]$Id){return $script:currentTask}
    function Invoke-ThunderApi($Connection,$Method,$Arguments){if($Method -eq 'TaskManagerFindPanTaskByFileId'){return @{data=$script:lookup}};throw "不应调用本机接口：$Method"}
    function Invoke-PanApi($Connection,$Method,$Arguments){
        if($script:forceTimeout){throw '迅雷接口响应超时；须核对实际状态后再恢复'}
        if($Method -eq 'batchCreateDownloadTaskWithFiles'){$script:createCalls++;return @{taskIds=@(42)}}
        if($Method -eq 'batchMoveFiles'){$script:moveCalls++;return @{task_id='move'}}
        if($Method -eq 'getFileInfo'){return @{id='file';kind='drive#file';name='测试.mkv';size=100;user_id='owner';parent_id=$script:currentParent;trashed=$false}}
        throw "不应调用云盘接口：$Method"
    }
    $job=@{id='1';folderName='任务-1';folderId='job';downloads=@{};stage='manual';deadline=0;btFiles=@(@{fileName='测试.mkv';filePath='';fileSize=100;downloadSize=0;download=$false});original=@{savePath=$testDir};name='资源'}
    $leaf=@{file=@{id='file';name='测试.mkv';size=100;parent_id='source'};relative='资源\测试.mkv'}
    Process-ReadyFile $null $job $leaf
    Assert-Test ($job.downloads.file.stage -eq 'unselected' -and $script:createCalls -eq 0) '原BT未勾选文件不下载'
    function Get-ReadyLeaves {throw '窗口结束后不得再次扫描云盘缓存'}
    Resume-Job $null $job
    Assert-Test ($script:createCalls -eq 0) '窗口结束后不发现或下载后续缓存'
    # 以下测试只模拟接口，不连接迅雷或修改真实云盘。
    function Get-MoveTarget($Connection,$Job,$Record){return 'destination'}
    function Assert-OwnedDescendant($Connection,$Folder,$Ancestor){if($Folder -ne 'source' -or $Ancestor -ne 'job'){throw '来源不属于本任务'}}
    $record=@{fileId='file';parentId='source';name='测试.mkv';size=100;relative='资源\测试.mkv';stage='created';taskId=42;intent=$true;initialBytes=0;savePath='D:\测试';moveIntent=$false;startedAt=0}
    Update-Download $null $job $record
    Assert-Test ($script:moveCalls -eq 0 -and $record.stage -eq 'created') '排队或尚未收数据时不移动'
    $script:currentTask.taskStatus=5;$script:currentTask.downloadSize=10;$script:currentTask.downloadSpeed=10
    Update-Download $null $job $record
    Assert-Test ($script:moveCalls -eq 1 -and $record.moveIntent) '收到数据后才提交单文件移动'
    Update-Download $null $job $record
    Assert-Test ($script:moveCalls -eq 1) '移动响应丢失或未完成时不重复提交'
    $script:currentParent='destination'
    Update-Download $null $job $record
    Assert-Test ($record.stage -eq 'moved' -and $record.fileId -eq 'file' -and $script:moveCalls -eq 1) '重启可按实际父目录确认移动并保留文件ID'
    $record.stage='created';$record.taskId=0;$script:lookup=42
    Update-Download $null $job $record
    Assert-Test ($record.taskId -eq 42 -and $script:createCalls -eq 0) '创建响应丢失后按文件ID恢复且不重复下载'
    $script:currentParent='other';$record.stage='created';$record.moveIntent=$false;$caught=$false
    try{Update-Download $null $job $record}catch{$caught=$true}
    Assert-Test ($caught -and $script:moveCalls -eq 1) '文件被外部移走时不再移动'
    $script:currentParent='source';$script:State.ownerId='other';$caught=$false
    try{Update-Download $null $job $record}catch{$caught=$true}
    Assert-Test ($caught -and $script:moveCalls -eq 1) '云盘归属不符时不移动'
    $script:State.ownerId='owner';$script:currentTask.isPanTask=$false
    Update-Download $null $job $record
    Assert-Test ($record.stage -eq 'review' -and $script:moveCalls -eq 1) '本地任务类型不符时保留云盘文件'
    $script:currentTask.isPanTask=$true;$script:forceTimeout=$true;$record.stage='created';$job.downloads=@{file=$record};$caught=$false
    try{Resume-Job $null $job}catch{$caught=$true}
    Assert-Test $caught '接口响应超时交给外层重连并停止当前连接上的操作'
    # 使用模拟时钟验证连续停滞，避免真实等待十分钟或操作任何下载。
    $script:clock=1000;$script:transfers=0
    $script:candidate=@{taskId=[long]99;taskName='规则验证';taskType=2;taskStatus=5;downloadSize=[long]0;downloadSpeed=[long]0;fileSize=[long]100;recycleTime=[long]0;isPanTask=$false;savePath='D:\测试';url='magnet:?xt=urn:btih:测试';origin=''}
    function Get-Now {return $script:clock}
    function Get-ActiveTasks($Connection){return $script:candidate}
    function Get-LocalTask($Connection,[long]$Id){return $script:candidate}
    function New-Job($Connection,$Task){$script:transfers++;$new=@{id=[string]$Task.taskId;stage='manual';downloads=@{}};$script:State.jobs[$new.id]=$new;return $new}
    function Start-Job($Connection,$Job) {}
    function Resume-Job($Connection,$Job) {}
    $script:State.jobs=@{};$script:State.observations=@{}
    Invoke-Round $null
    for($tick=1015;$tick -le 1585;$tick+=15){$script:clock=$tick;Invoke-Round $null}
    $script:clock=1599;Invoke-Round $null
    Assert-Test ($script:transfers -eq 0) '停滞未满十分钟不转存'
    $script:clock=1600;Invoke-Round $null
    Assert-Test ($script:transfers -eq 1) '连续零速且字节不增长满十分钟才转存'
    $script:State.jobs=@{};$script:State.observations=@{};$script:transfers=0;$script:clock=2000
    Invoke-Round $null
    for($tick=2015;$tick -le 2495;$tick+=15){$script:clock=$tick;Invoke-Round $null}
    $script:clock=2500;$script:candidate.downloadSize=[long]1;Invoke-Round $null
    for($tick=2515;$tick -le 2590;$tick+=15){$script:clock=$tick;Invoke-Round $null}
    $script:clock=2600;Invoke-Round $null
    Assert-Test ($script:transfers -eq 0 -and $script:State.observations['99'].first -eq 2500) '下载字节增加后重新计算停滞时间'
    $script:candidate.downloadSpeed=[long]1;Invoke-Round $null
    Assert-Test ($script:State.observations.Count -eq 0) '恢复速度后取消停滞观察'
    $script:candidate.downloadSpeed=[long]0;$script:candidate.isPanTask=$true
    Assert-Test (-not (Test-Candidate $script:candidate)) '云盘下载任务不被再次转存'
    @{passed=$script:passed.Count;tests=$script:passed;liveCloudCalls=0}|ConvertTo-Json -Depth 5
} finally {Remove-Item -LiteralPath $testDir -Recurse -Force -ErrorAction SilentlyContinue}
