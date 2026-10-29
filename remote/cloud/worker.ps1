[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$InputPath)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$p=Get-Content -Raw -Encoding UTF8 -LiteralPath $InputPath|ConvertFrom-Json
$statusPath=$p.statusPath
$script:WebProgress=@{phase='initializing';processed=0;total=@($p.expected).Count;current=''}
function Save-WebStatus($stage,$result,$message='') {
    $value=@{id=$p.id;state=$stage;updatedAt=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds();result=$result;error=$message;progress=$script:WebProgress}
    $tmp=$statusPath+'.tmp'
    [IO.File]::WriteAllText($tmp,($value|ConvertTo-Json -Depth 40 -Compress),[Text.UTF8Encoding]::new($true))
    if(Test-Path -LiteralPath $statusPath){[IO.File]::Replace($tmp,$statusPath,$statusPath+'.bak')}else{[IO.File]::Move($tmp,$statusPath)}
}
$connection=$null;$mutex=$null;$owned=$false
try {
    Save-WebStatus 'running' $null
    . (Join-Path $PSScriptRoot 'manual.ps1') -WorkDir $p.workDir -LibraryOnly
    $script:WebProgress.phase='waiting_for_mutex';Save-WebStatus 'running' $null
    $mutex=[Threading.Mutex]::new($false,'Global\ThunderCloudTransferV1')
    try{$owned=$mutex.WaitOne(30000)}catch [Threading.AbandonedMutexException]{$owned=$true}
    if(-not $owned){throw '其他转存命令正在执行，请稍后核对；本次未操作任务'}
    Read-State
    if($script:Config.deleteOriginalFiles -ne $false){throw 'WebUI要求保留原本地文件'}
    $script:WebProgress.phase='connecting_client';Save-WebStatus 'running' $null
    $connection=Open-ThunderConnection
    $script:WebProgress.phase='checking';Save-WebStatus 'running' $null
    # Publish each durable task transition while retaining the engine's atomic
    # state writes. No parallel access to its mutable state or native pipe.
    $script:NativeSaveState=${function:Save-State}
    function Save-State {
        & $script:NativeSaveState
        $script:WebProgress.processed=@($p.expected|Where-Object {
            $j=$script:State.jobs[[string]$_.id];$j -and ($j.stage -in @('caching','manual') -or $j.error)
        }).Count
        $j=$script:State.jobs[[string]$script:WebProgress.current]
        if($j){$script:WebProgress.phase=[string]$j.stage}
        Save-WebStatus 'running' $null
    }
    $script:NativeStartJob=${function:Start-Job}
    function Start-Job($Connection,$Job) {
        $script:WebProgress.current=[string]$Job.id;$script:WebProgress.phase='prepared';Save-WebStatus 'running' $null
        & $script:NativeStartJob $Connection $Job
    }
    # Bind a confirmed preview to exact identities. Subsequent queue entries are
    # never included; the engine rechecks speed, progress and identity again.
    $script:WebExpected=@{}
    foreach($t in @($p.expected)){$script:WebExpected[[string]$t.id]=$t}
    function Get-WebTasks($Connection,[int]$View) {
        $list=(Invoke-ThunderApi $Connection 'CategoryManagerCategoryViewGetTasks' @(-1,$View)).data
        if($null -eq $list){throw '缺少当前任务视图'}
        $active=@()
        if($View -eq 1){
            $reply=(Invoke-ThunderApi $Connection 'CategoryManagerCategoryGetDownloadingTaskIds' @(-1)).data
            if($null -eq $reply){throw '缺少当前下载列表'}
            $active=@($reply)
        }
        foreach($key in @($script:WebExpected.Keys)) {
            $e=$script:WebExpected[$key];$id=[long]$e.id
            if($id -notin @($list) -or ($View -eq 1 -and $id -notin $active)){continue}
            $t=(Invoke-ThunderApi $Connection 'TaskManagerTaskGetTaskBase' @($id)).data
            if($t.taskId -ne $id -or $t.taskName -cne $e.name -or $t.savePath -cne $e.save_path -or
                [string]$t.url -cne [string]$e.url -or $t.fileSize -ne $e.size){continue}
            if($View -eq 3 -and $t.recycleTime -gt 0 -and -not $t.isPanTask -and $t.taskType -in @(1,2,5)){$t}
            elseif($View -eq 1 -and $t.recycleTime -eq 0){$t}
        }
    }
    function Get-ActiveTasks($Connection){Get-WebTasks $Connection 1}
    function Get-RecycleTasks($Connection){Get-WebTasks $Connection 3}
    $options=@{command=$p.command;workDir=$p.workDir;limit=[double]$p.options.progress_limit;
        ids=@($p.expected|ForEach-Object {[long]$_.id});all=$false;output=[string]$p.options.output;
        folder=[string]$p.options.folder;wait=[int]$p.options.wait_minutes;cacheOnly=[bool]$p.options.cache_only;
        dry=$false;request='';library=$false;cleanupRecycle=($p.command -eq 'Recycle' -and $p.cleanupRecycle -eq $true)}
    $script:ManualOptions=$options
    if($p.command -in @('Transfer','Recycle') -and -not $options.ids.Count){throw '已确认列表为空，不执行转存'}
    if($p.command -eq 'Download') {
        if(-not $options.folder){$options.folder=[string]$script:State.rootFolderId}
        if(-not $options.folder){throw '当前工作空间尚无转存目录，请重新预览'}
        # Download only the resource units confirmed in the preview.
        $script:WebUnits=@($p.units|ForEach-Object {$_.id})
        $script:WebFiles=@{}
        foreach($u in @($p.units)){$script:WebFiles[[string]$u.id]=@($u.fileIds)}
        $script:NativeGetCompleteSnapshot=${function:Get-CompleteSnapshot}
        function Get-CompleteSnapshot($Connection,$Job) {
            $snapshot=& $script:NativeGetCompleteSnapshot $Connection $Job
            $expected=@($script:WebFiles[[string]$Job.id]|Sort-Object)
            $current=@($snapshot.leaves|ForEach-Object {[string]$_.file.id}|Sort-Object)
            if(($expected -join ',') -cne ($current -join ',')){return @{ready=$false;reason='文件列表与预览不同，请重新预览';leaves=@()}}
            return $snapshot
        }
        $script:NativeGetCloudUnits=${function:Get-CloudUnits}
        function Get-CloudUnits($Connection,[string]$FolderId,[int]$Depth=0) {
            @(& $script:NativeGetCloudUnits $Connection $FolderId $Depth)|Where-Object {$_.id -in $script:WebUnits}
        }
    }
    if($p.command -eq 'Sync') {
        $ids=@($p.units|ForEach-Object {$_.id})
        $jobs=@($script:State.jobs.Values|Where-Object {$_.id -in $ids})
        Sync-RequestedDownloads $connection $jobs
        $result=@{summary=(Get-Summary);note='仅核对已有下载，不创建新下载或删除资源'}
    } else {$result=Invoke-ManualCommand $connection $options}
    $script:WebProgress.phase='submitted';Save-WebStatus 'done' $result
} catch {
    # Keep raw remote errors in the namespace on the machine. The web backend
    # only returns a bounded, sanitized message, never SDK authentication data.
    Save-WebStatus 'error' $null '远端执行失败，请查看请求及转存状态；不要重复提交'
} finally {
    Close-ThunderConnection $connection
    if($owned){$mutex.ReleaseMutex()};if($mutex){$mutex.Dispose()}
    if($p.scheduledTask){Unregister-ScheduledTask -TaskName $p.scheduledTask -Confirm:$false -ErrorAction SilentlyContinue}
}
