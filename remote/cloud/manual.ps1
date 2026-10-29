[CmdletBinding()]
param(
    [ValidateSet('Transfer','Recycle','Download','ListRecycle','ListCloud','Due','CleanRecycle','Requests')][string]$Command='Requests',
    [string]$WorkDir=$PSScriptRoot,
    [double]$ProgressLimit=20,
    [long[]]$TaskIds=@(),
    [switch]$All,
    [string]$OutputRoot='',
    [string]$CloudFolderId='',
    [ValidateRange(0,1440)][int]$WaitMinutes=5,
    [switch]$CacheOnly,
    [switch]$DryRun,
    [string]$RequestId='',
    [switch]$LibraryOnly
)
# 先保存入口参数，点加载公共库时其参数变量会进入当前作用域。
$manualOptions=@{command=$Command;workDir=$WorkDir;limit=$ProgressLimit;ids=$TaskIds;all=[bool]$All;output=$OutputRoot;folder=$CloudFolderId;wait=$WaitMinutes;cacheOnly=[bool]$CacheOnly;dry=[bool]$DryRun;request=$RequestId;library=[bool]$LibraryOnly}
. (Join-Path $PSScriptRoot 'engine.ps1') -WorkDir $WorkDir -LibraryOnly
$script:ManualOptions=$manualOptions
$script:ManualEntry=Join-Path $PSScriptRoot 'manual.ps1'

function Test-ManualCandidate($Task,[double]$Limit) {
    if($null -eq $Task -or $Task.isPanTask -or $Task.origin -eq 'cloud_bridge_v1'){return $false}
    foreach($key in @('taskId','taskType','taskStatus','downloadSize','downloadSpeed','fileSize','recycleTime')) {
        if($null -eq $Task.$key -or $Task.$key -isnot [ValueType]){return $false}
    }
    return ($Task.taskId -gt 0 -and $Task.taskType -in @(1,2,5) -and $Task.taskStatus -eq 5 -and $Task.recycleTime -eq 0 -and
        $Task.downloadSpeed -eq 0 -and $Task.fileSize -gt 0 -and $Task.downloadSize -ge 0 -and
        [decimal]$Task.downloadSize*100 -lt [decimal]$Task.fileSize*$Limit -and -not [string]::IsNullOrWhiteSpace($Task.savePath))
}
function Assert-OutputPath([string]$Path) {
    if($Path -notmatch '^[a-zA-Z]:\\' -and $Path -notmatch '^\\\\[^\\]+\\[^\\]+\\?'){throw '下载位置必须是目标 Windows 机器的绝对路径，例如 D:\下载'}
    foreach($part in @($Path.TrimEnd('\').Substring(3).Split('\'))){if($part){$null=Get-SafeSegment $part}}
}
function Get-RecycleTasks($Connection) {
    $ids=(Invoke-ThunderApi $Connection 'CategoryManagerCategoryViewGetTasks' @(-1,3)).data
    if($null -eq $ids){throw '未返回迅雷回收站列表'}
    foreach($id in $ids) {
        $t=(Invoke-ThunderApi $Connection 'TaskManagerTaskGetTaskBase' @($id)).data
        if($t.taskId -eq $id -and $t.recycleTime -gt 0 -and -not $t.isPanTask -and $t.taskType -in @(1,2,5)){$t}
    }
}
function New-BatchFolderName([string]$Id) {
    if($Id -notmatch '^[a-f0-9]{32}$'){throw '批次请求编号格式错误'}
    # 时间便于人工查找，请求编号保证同一秒发出的不同批次也不会合并。
    return ([DateTimeOffset]::FromUnixTimeSeconds((Get-Now)).LocalDateTime.ToString('yyyy-MM-dd_HH-mm-ss')+'-'+$Id.Substring(0,8))
}
function New-ManualJob($Connection,$Task,[string]$Source,$Options) {
    $key=[string]$Task.taskId
    if($script:State.jobs.ContainsKey($key)){throw '该任务已有转存记录；请使用云盘下载处理，避免重复提交'}
    if($Source -eq 'active' -and -not (Test-ManualCandidate $Task $Options.limit)){throw '任务已不符合 zero 筛选规则'}
    if($Source -eq 'recycle' -and ($Task.recycleTime -le 0 -or $Task.isPanTask -or $Task.taskType -notin @(1,2,5))){throw '不是可转存的迅雷回收站任务'}
    if(-not $Options.output){Assert-OutputPath ([string]$Task.savePath)}
    $url=[string]$Task.url;$bt=@()
    if($Task.taskType -in @(2,5)) {
        $bt=@((Invoke-ThunderApi $Connection 'TaskManagerTaskBtTaskGetBtFileInfos' @($Task.taskId)).data)
        if(-not $bt.Count -or -not @($bt|Where-Object {$_.download -eq $true}).Count){throw '无法保存 BT 文件清单及选择，保留原任务'}
        if($url -notmatch '^(magnet:\?|https?://|ed2k://|thunder://)') {
            $hash=(Invoke-ThunderApi $Connection 'TaskManagerTaskBtTaskGetInfoHash' @($Task.taskId)).data
            if([string]$hash -notmatch '^[a-fA-F0-9]{40}$'){throw '没有可转存的原链接或磁力哈希'}
            $url='magnet:?xt=urn:btih:'+$hash
        }
    }
    if($url -notmatch '^(magnet:\?|https?://|ed2k://|thunder://)'){throw '原链接不能用于云缓存'}
    $job=@{id=$key;name=$Task.taskName;original=ConvertTo-Map $Task;url=$url;btFiles=@($bt|ForEach-Object {ConvertTo-Map $_});
        stage='prepared';createdAt=Get-Now;folderName=('任务-'+$key);folderId='';cacheTaskId='';cacheIntent=$false;cacheFileId='';
        deadline=0;downloads=@{};error='';manualRemaining=$true;deleteIntent=$false;originalDeleted=$false;
        source=$Source;manualMode=$true;progressLimit=$Options.limit;waitSeconds=60*$Options.wait;downloadNotBefore=0;
        cleanupRecycle=($Source -eq 'recycle' -and $Options.cleanupRecycle -eq $true);recycleCleanup='waiting';cacheAcknowledged=$false;
        outputRoot=$Options.output;cacheOnly=$Options.cacheOnly;requestId=$Options.request;batchFolderName=$Options.batchFolderName;batchFolderId=''}
    $script:State.jobs[$key]=$job;Save-State
    return $job
}
function Get-RequestPath([string]$Id) {
    if($Id -notmatch '^[a-f0-9]{32}$'){throw '请求编号格式错误'}
    return Join-Path $script:RecordDir ('请求模块-'+$Id+'-v1.json')
}
function Save-Request($Request) {
    $path=Get-RequestPath $Request.id
    New-Item -ItemType Directory -Path $script:RecordDir -Force|Out-Null
    $tmp=$path+'.tmp';[IO.File]::WriteAllText($tmp,($Request|ConvertTo-Json -Depth 20),[Text.UTF8Encoding]::new($true))
    if(Test-Path -LiteralPath $path){[IO.File]::Replace($tmp,$path,$path+'.bak')}else{[IO.File]::Move($tmp,$path)}
}
function Read-Request([string]$Id){return ConvertTo-Map (Get-Content -Raw -Encoding UTF8 -LiteralPath (Get-RequestPath $Id)|ConvertFrom-Json)}
function Register-Once($Request,[long]$At) {
    $atDate=[DateTimeOffset]::FromUnixTimeSeconds($At).LocalDateTime
    # 仅登记一次触发器，不设重复周期或登录后监控。
    $entry=$script:ManualEntry.Replace("'","''");$dir=$script:ManualOptions.workDir.Replace("'","''")
    $body="& '$entry' -Command Due -WorkDir '$dir' -RequestId '$($Request.id)'"
    $encoded=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($body))
    $action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $encoded" -WorkingDirectory $script:ManualOptions.workDir
    $trigger=New-ScheduledTaskTrigger -Once -At $atDate
    $principal=New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Highest
    $settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
    $name='迅雷云盘模块-单次请求-'+$Request.id
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force|Out-Null
    $Request.scheduledTask=$name;$Request.notBefore=$At;Save-Request $Request
}
function Register-RecycleCleanup($Request,[long]$At) {
    $entry=$script:ManualEntry.Replace("'","''");$dir=$script:ManualOptions.workDir.Replace("'","''")
    $body="& '$entry' -Command CleanRecycle -WorkDir '$dir' -RequestId '$($Request.id)'"
    $encoded=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($body))
    $name='ThunderWebUI-RecycleCleanup-'+$Request.id
    $action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $encoded" -WorkingDirectory $script:ManualOptions.workDir
    $trigger=New-ScheduledTaskTrigger -Once -At ([DateTimeOffset]::FromUnixTimeSeconds($At).LocalDateTime)
    $principal=New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Highest
    $settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force|Out-Null
    $Request.cleanupScheduledTask=$name;$Request.cleanupNextCheck=$At;Save-Request $Request
}
function Clear-TransferredRecycle($Connection,$Job,$Snapshot) {
    if($Job.source -ne 'recycle' -or $Job.cleanupRecycle -ne $true){return}
    if($Job.recycleCleanup -in @('cleaned','absent','changed','uncertain','expired')){return}
    # A createFile acknowledgement alone does not prove a successful cache.
    if($Job.cacheAcknowledged -ne $true -or -not $Snapshot.ready){return}
    $ids=(Invoke-ThunderApi $Connection 'CategoryManagerCategoryViewGetTasks' @(-1,3)).data
    if($null -eq $ids){throw '未返回回收站列表，保留原记录'}
    $id=[long]$Job.id
    if($id -notin @($ids)) {
        $Job.recycleCleanup=if($Job.recycleCleanupIntent){'cleaned'}else{'absent'}
        $Job.recycleCleanedAt=Get-Now;$Job.recycleCleanupError='';Save-State;return
    }
    # An ambiguous mutation is never sent a second time.
    if($Job.recycleCleanupIntent){$Job.recycleCleanup='uncertain';Save-State;return}
    $fresh=(Invoke-ThunderApi $Connection 'TaskManagerTaskGetTaskBase' @($id)).data
    $original=$Job.original
    if($fresh.taskId -ne $id -or $fresh.recycleTime -le 0 -or $fresh.recycleTime -ne $original.recycleTime -or
       $fresh.taskName -cne $original.taskName -or $fresh.savePath -cne $original.savePath -or
       $fresh.url -cne $original.url -or $fresh.fileSize -ne $original.fileSize -or $fresh.taskType -ne $original.taskType -or $fresh.isPanTask) {
        $Job.recycleCleanup='changed';Save-State;return
    }
    $Job.recycleCleanupIntent=$true;$Job.recycleCleanup='uncertain';Save-State
    Write-Event '回收站转存清理开始' @{id=$Job.id;folderId=$Job.folderId;deleteFiles=$false}
    # Native single-task deletion. Never use deleteAllTask / batchRecycleTasks.
    $r=(Invoke-ThunderApi $Connection 'TaskManagerTaskDeleteTask' @($id,$false)).data
    if($null -eq $r -or $r -ne 0){throw '回收站清理未确认，禁止自动重试'}
    $after=(Invoke-ThunderApi $Connection 'CategoryManagerCategoryViewGetTasks' @(-1,3)).data
    if($null -eq $after -or $id -in @($after)){throw '清理后回收站记录仍存在，须人工核对'}
    $Job.recycleCleanup='cleaned';$Job.recycleCleanedAt=Get-Now;$Job.recycleCleanupError='';Save-State
    Write-Event '回收站转存清理完成' @{id=$Job.id;folderId=$Job.folderId;deleteFiles=$false}
}
function Invoke-RecycleCleanup($Connection,$Request) {
    if($Request.cleanupRecycle -ne $true){return @{cleanupStage='disabled'}}
    $pending=0;$cleaned=0;$review=0
    foreach($id in @($Request.jobIds)) {
        $job=$script:State.jobs[[string]$id]
        if(-not $job -or $job.source -ne 'recycle' -or $job.cleanupRecycle -ne $true){continue}
        if($job.recycleCleanup -notin @('cleaned','absent','changed','uncertain','expired')) {
            if($Request.cleanupDeadline -and (Get-Now) -ge $Request.cleanupDeadline){$job.recycleCleanup='expired';Save-State}
            elseif($job.cacheAcknowledged -eq $true) {
                try {
                    $snapshot=Get-CompleteSnapshot $Connection $job
                    Clear-TransferredRecycle $Connection $job $snapshot
                } catch {
                    $job.recycleCleanupError=$_.Exception.Message;Save-State
                    if(Test-ConnectionFault $_){throw}
                }
            }
        }
        if($job.recycleCleanup -in @('cleaned','absent')){$cleaned++}
        elseif($job.recycleCleanup -in @('changed','uncertain','expired')){$review++}
        else{$pending++}
    }
    # If the submit worker crashed before publishing jobIds, retain everything.
    if($Request.stage -eq 'submitting' -and $pending -eq 0 -and (Get-Now) -lt $Request.cleanupDeadline){$pending=1}
    $Request.cleanupPending=$pending;$Request.cleanupCleaned=$cleaned;$Request.cleanupReview=$review
    $Request.cleanupStage=if($pending){'waiting'}elseif($review){'review'}else{'done'}
    Save-Request $Request
    if($pending){Register-RecycleCleanup $Request ((Get-Now)+300)}
    elseif($Request.cleanupScheduledTask){Unregister-ScheduledTask -TaskName $Request.cleanupScheduledTask -Confirm:$false -ErrorAction SilentlyContinue}
    return @{cleanupStage=$Request.cleanupStage;pending=$pending;cleaned=$cleaned;review=$review}
}
function Get-CloudTree($Connection,[string]$Parent,[string]$Relative='', [int]$Depth=0) {
    if($Depth -gt 20){throw '云盘目录超过20层'}
    foreach($file in @(Get-Children $Connection $Parent)) {
        if($file.user_id -ne $script:State.ownerId -or $file.parent_id -ne $Parent){throw '云盘归属或父目录不匹配'}
        if($file.kind -eq 'drive#folder' -and $file.name -in @('已下载','已开始下载')){continue}
        $name=Get-SafeSegment $file.name;$relativePath=if($Relative){$Relative+'\'+$name}else{$name}
        if($file.kind -eq 'drive#folder') {
            if($file.phase -and $file.phase -ne 'PHASE_TYPE_COMPLETE'){@{file=$file;relative=$relativePath;pending=$true}}
            Get-CloudTree $Connection $file.id $relativePath ($Depth+1)
        } else {@{file=$file;relative=$relativePath;pending=($file.kind -ne 'drive#file' -or $file.phase -ne 'PHASE_TYPE_COMPLETE')}}
    }
}
function Get-CompleteSnapshot($Connection,$Job) {
    if($Job.singleFileId) {
        $f=Invoke-PanApi $Connection 'getFileInfo' @($Job.singleFileId,@{params=@{space='';usage='DISPLAY'}})
        if($f.trashed -or $f.user_id -ne $script:State.ownerId -or $f.parent_id -ne $Job.folderId){throw '云盘单文件已变化'}
        $leaves=@(@{file=$f;relative=(Get-SafeSegment $f.name);pending=($f.phase -ne 'PHASE_TYPE_COMPLETE')})
    } else {
        $info=Invoke-PanApi $Connection 'getFileInfo' @($Job.folderId,@{params=@{space='';usage='DISPLAY'}})
        if($info.trashed -or $info.user_id -ne $script:State.ownerId -or $info.kind -ne 'drive#folder'){throw '资源目录不可用或账号已变化'}
        if($info.phase -and $info.phase -ne 'PHASE_TYPE_COMPLETE'){return @{ready=$false;reason='资源目录尚未缓存完成';leaves=@()}}
        if($Job.cacheFileId) {
            $root=Invoke-PanApi $Connection 'getFileInfo' @($Job.cacheFileId,@{params=@{space='';usage='DISPLAY'}})
            if($root.trashed -or $root.user_id -ne $script:State.ownerId){throw '缓存根资源不可用'}
            if($root.phase -ne 'PHASE_TYPE_COMPLETE'){return @{ready=$false;reason='缓存根资源尚未完成';leaves=@()}}
        }
        $leaves=@(Get-CloudTree $Connection $Job.folderId)
    }
    if(-not $leaves.Count -or @($leaves|Where-Object {$_.pending}).Count){return @{ready=$false;reason='存在未缓存完成的文件，或尚未获得文件清单';leaves=@()}}
    if($Job.btFiles.Count) {
        # 同时核对原始清单，避免目录暂时只列出已完成子文件时误判整项完成。
        $known=@($leaves)+@($Job.downloads.Values|ForEach-Object {@{relative=$_.relative;file=@{size=$_.size}}})
        foreach($bt in $Job.btFiles) {
            $name=([string]$bt.fileName).Replace('/','\');$path=([string]$bt.filePath).Replace('/','\').Trim('\')
            $full=if($path){$path+'\'+$name}else{$name}
            $matches=@($known|Where-Object {([long]$_.file.size -eq [long]$bt.fileSize) -and ($_.relative -eq $full -or $_.relative.EndsWith('\'+$full))})
            if(-not $matches.Count){return @{ready=$false;reason=('原 BT 清单仍缺少文件：'+$full);leaves=@()}}
        }
    }
    return @{ready=$true;reason='整项缓存完成';leaves=$leaves}
}
function Get-TrackedFiles {
    $tracked=@{}
    foreach($j in $script:State.jobs.Values){foreach($r in $j.downloads.Values){if($r.intent -or $r.stage -in @('moved','alreadyLocal','created','started','external','review')){$tracked[[string]$r.fileId]=$true}}}
    return $tracked
}
function Invoke-WholeDownload($Connection,$Job,$Snapshot) {
    if(-not $Snapshot.ready){$Job.lastCheckReason=$Snapshot.reason;$Job.stage='manual';Save-State;return}
    try{Clear-TransferredRecycle $Connection $Job $Snapshot}catch{$Job.recycleCleanupError=$_.Exception.Message;Save-State;if(Test-ConnectionFault $_){throw}}
    $tracked=Get-TrackedFiles
    foreach($leaf in $Snapshot.leaves) {
        if($tracked.ContainsKey([string]$leaf.file.id) -and -not $Job.downloads.ContainsKey([string]$leaf.file.id)){continue}
        try{Process-ReadyFile $Connection $Job $leaf}
        catch{$r=$Job.downloads[[string]$leaf.file.id];if($r){$r.error=$_.Exception.Message;if(-not $r.intent){$r.stage='review'}};$Job.error=$_.Exception.Message;Save-State;if(Test-ConnectionFault $_){throw}}
    }
    $Job.stage='manual';$Job.lastCheckReason='已检查整项并提交可下载文件';Save-State
}
function Sync-RequestedDownloads($Connection,[object[]]$Jobs) {
    # 仅短暂确认本次创建任务的启动与移动；不再次扫描云缓存，也不持续后台运行。
    $until=(Get-Now)+[int]$script:Config.startGraceSeconds
    do {
        $pending=0
        foreach($job in $Jobs){foreach($record in @($job.downloads.Values)) {
            if($record.stage -in @('ready','created','started','alreadyLocal')) {
                try{Update-Download $Connection $job $record}catch{$record.error=$_.Exception.Message;Save-State;if(Test-ConnectionFault $_){throw}}
                if($record.stage -in @('created','started','alreadyLocal')){$pending++}
            }
        }}
        if(-not $pending -or (Get-Now) -ge $until){break}
        Start-Sleep -Seconds 3
    }while($true)
}
function Get-CloudUnits($Connection,[string]$FolderId,[int]$Depth=0) {
    if($Depth -gt 22){throw '云盘批次目录层级超过22层'}
    $batchIds=@($script:State.jobs.Values|ForEach-Object {if($_.batchFolderId){[string]$_.batchFolderId}})
    if($script:State.batches){$batchIds+=@($script:State.batches.Values|ForEach-Object {if($_.folderId){[string]$_.folderId}})}
    foreach($f in @(Get-Children $Connection $FolderId)) {
        if($f.user_id -ne $script:State.ownerId){continue}
        if($f.kind -eq 'drive#folder' -and $f.name -in @('已下载','已开始下载')){continue}
        if($f.kind -eq 'drive#folder' -and ($f.id -eq $script:State.rootFolderId -or $f.id -in $batchIds)){Get-CloudUnits $Connection $f.id ($Depth+1);continue}
        $existing=@($script:State.jobs.Values|Where-Object {$_.folderId -eq $f.id -and -not $_.singleFileId})
        if($existing.Count -eq 1){$existing[0];continue}
        if($existing.Count -gt 1){throw '同一云盘目录出现多条转存记录'}
        $id='cloud-'+[string]$f.id
        if($script:State.jobs.ContainsKey($id)){$script:State.jobs[$id];continue}
        $name=Get-SafeSegment $f.name
        @{id=$id;name=$name;folderId=$(if($f.kind -eq 'drive#folder'){$f.id}else{$f.parent_id});
            singleFileId=$(if($f.kind -eq 'drive#file'){$f.id}else{''});folderName=($name+'--'+$f.id);btFiles=@();downloads=@{};original=@{};
            manualMode=$true;stage='manual';source='cloud';outputRoot='';error='';originalDeleted=$false}
    }
}
function Assert-ScanScope($Connection,[string]$Folder) {
    for($depth=0;$Folder -and $depth -le 22;$depth++) {
        $f=Invoke-PanApi $Connection 'getFileInfo' @($Folder,@{params=@{space='';usage='DISPLAY'}})
        if($f.kind -ne 'drive#folder' -or $f.trashed -or $f.user_id -ne $script:State.ownerId){throw '扫描目录归属不匹配'}
        if($f.name -in @('已下载','已开始下载') -or $f.id -in @($script:State.downloadedFolderId,$script:State.startedFolderId)){throw '已下载归档目录已排除，请选择待下载目录'}
        $Folder=[string]$f.parent_id
    }
    if($Folder){throw '扫描目录父级超过22层'}
}
function Initialize-CloudOwner($Connection) {
    if($script:State.ownerId){$null=Get-OwnedFolder $Connection;return}
    $root=@(Get-Children $Connection '')
    if($root.Count -and $root[0].user_id){$script:State.ownerId=[string]$root[0].user_id}
    elseif($script:ManualOptions.folder){$f=Invoke-PanApi $Connection 'getFileInfo' @($script:ManualOptions.folder,@{params=@{space='';usage='DISPLAY'}});$script:State.ownerId=[string]$f.user_id}
    if(-not $script:State.ownerId){throw '云盘为空或无法确定登录账号'}
}
function Invoke-ManualCommand($Connection,$Options) {
    if($Options.command -eq 'CleanRecycle'){return Invoke-RecycleCleanup $Connection (Read-Request $Options.request)}
    if($Options.command -eq 'ListRecycle') {
        return @{tasks=@(Get-RecycleTasks $Connection|Select-Object taskId,taskName,taskType,recycleTime,savePath);note='迅雷本地任务回收站；不会恢复或清空'}
    }
    if($Options.command -in @('Transfer','Recycle')) {
        if($Options.output){Assert-OutputPath $Options.output}
        $tasks=if($Options.command -eq 'Recycle'){@(Get-RecycleTasks $Connection)}else{@(Get-ActiveTasks $Connection|Where-Object {Test-ManualCandidate $_ $Options.limit})}
        if($Options.ids.Count){$tasks=@($tasks|Where-Object {$_.taskId -in $Options.ids})}
        elseif($Options.command -eq 'Recycle' -and -not $Options.all){throw '回收转存必须指定任务ID或 all'}
        $tasks=@($tasks|Where-Object {-not $script:State.jobs.ContainsKey([string]$_.taskId)})
        if($Options.dry){return @{dryRun=$true;matched=$tasks.Count;waitMinutes=$Options.wait;cacheOnly=$Options.cacheOnly;tasks=@($tasks|Select-Object taskId,taskName,savePath)}}
        $request=@{id=[Guid]::NewGuid().ToString('N');type=$Options.command;createdAt=Get-Now;jobIds=@();errors=@();stage='submitting';cacheOnly=$Options.cacheOnly;waitMinutes=$Options.wait;notBefore=0}
        $request.cleanupRecycle=($Options.command -eq 'Recycle' -and $Options.cleanupRecycle -eq $true)
        if($request.cleanupRecycle){$request.cleanupDeadline=(Get-Now)+86400;$request.cleanupStage=if($tasks.Count){'waiting'}else{'done'}}
        $request.batchFolderName=New-BatchFolderName $request.id
        $Options.request=$request.id;$Options.batchFolderName=$request.batchFolderName;Save-Request $request
        # 在第一个删除或缓存提交前登记恢复入口；SSH 断开后仍能执行一次。
        if(-not $Options.cacheOnly -and $tasks.Count){Register-Once $request ((Get-Now)+60*$Options.wait+10)}
        if($request.cleanupRecycle -and $tasks.Count){Register-RecycleCleanup $request ((Get-Now)+300)}
        try {
            foreach($task in $tasks) {
                try {
                    $source=if($Options.command -eq 'Recycle'){'recycle'}else{'active'}
                    $job=New-ManualJob $Connection $task $source $Options
                    $request.jobIds+=,$job.id;Save-Request $request
                    Start-Job $Connection $job
                } catch {
                    $request.errors+=,@{taskId=$task.taskId;message=$_.Exception.Message};Save-Request $request
                    if(Test-ConnectionFault $_){throw}
                }
            }
        } finally {
            $request.stage=if(-not $request.jobIds.Count){if($request.errors.Count){'error'}else{'empty'}}elseif($Options.cacheOnly){'cacheOnly'}else{'waiting'};Save-Request $request
            if(-not $Options.cacheOnly -and $request.jobIds.Count) {
                $due=(Get-Now)+60*$Options.wait
                Register-Once $request $due
            } elseif($request.scheduledTask){Unregister-ScheduledTask -TaskName $request.scheduledTask -Confirm:$false -ErrorAction SilentlyContinue}
        }
        return @{request=$request;failed=$request.errors.Count;note=$(if($Options.cacheOnly){'只添加云盘，不安排下载；原本地文件保留'}else{"$($Options.wait)分钟后整项检查一次，未全部缓存完成的留到下次；原本地文件保留"})}
    }
    if($Options.command -eq 'Due') {
        $request=Read-Request $Options.request
        if($request.stage -in @('checked','cacheOnly')){return @{requestId=$request.id;stage=$request.stage}}
        if($request.stage -in @('checking','error')){return @{requestId=$request.id;stage=$request.stage;note='上次检查已消费或异常，不自动重新扫描；请使用云盘下载手动处理'}}
        if((Get-Now) -lt $request.notBefore){Register-Once $request ([long]$request.notBefore);return @{stage='waiting';notBefore=$request.notBefore}}
        $request.stage='checking';$request.checkStartedAt=Get-Now;Save-Request $request
        $jobs=@();$results=@()
        foreach($id in $request.jobIds) {
            $job=$script:State.jobs[[string]$id]
            if(-not $job){$results+=,@{id=$id;ready=$false;reason='缺少转存记录'};continue}
            if($job.stage -notin @('caching','manual')){$results+=,@{id=$id;ready=$false;reason='缓存提交状态不明，保留给下次核对'};continue}
            try{$snapshot=Get-CompleteSnapshot $Connection $job;Invoke-WholeDownload $Connection $job $snapshot;$results+=,@{id=$id;ready=$snapshot.ready;reason=$snapshot.reason};$jobs+=,$job}
            catch{$results+=,@{id=$id;ready=$false;reason=$_.Exception.Message};if(Test-ConnectionFault $_){throw}}
        }
        # 先持久化一次性检查已消费，后续恢复仅核对已提交下载，不重扫云缓存。
        $request.stage='checked';$request.checkedAt=Get-Now;$request.results=$results;Save-Request $request
        Sync-RequestedDownloads $Connection $jobs
        return @{request=$request;summary=(Get-Summary)}
    }
    if($Options.command -in @('Download','ListCloud')) {
        if($Options.command -eq 'Download'){Assert-OutputPath $Options.output}
        Initialize-CloudOwner $Connection
        Assert-ScanScope $Connection $Options.folder
        $jobs=@(Get-CloudUnits $Connection $Options.folder);$results=@();$handled=@();$tracked=Get-TrackedFiles
        foreach($job in $jobs) {
            try {
                $snapshot=Get-CompleteSnapshot $Connection $job
                $remaining=@($snapshot.leaves|Where-Object {-not $tracked.ContainsKey([string]$_.file.id)}).Count
                $results+=,@{id=$job.id;name=$job.name;folderId=$job.folderId;ready=$snapshot.ready;remaining=$remaining;reason=$snapshot.reason;fileIds=@($snapshot.leaves|ForEach-Object {[string]$_.file.id})}
                if($Options.command -eq 'Download' -and -not $Options.dry -and $snapshot.ready -and $remaining -gt 0) {
                    if(-not $script:State.jobs.ContainsKey($job.id)){$script:State.jobs[$job.id]=$job}
                    # 已有下载不改目标及移动意图；只有新发现的文件使用本次目标目录。
                    $job.outputRoot=$Options.output;$job.newArchiveMode=$true
                    if(-not $job.downloads.Count){$job.manualMode=$true}
                    Save-State;Invoke-WholeDownload $Connection $job $snapshot;$handled+=,$job;$tracked=Get-TrackedFiles
                }
            } catch {$results+=,@{id=$job.id;name=$job.name;ready=$false;reason=$_.Exception.Message};if(Test-ConnectionFault $_){throw}}
        }
        if($Options.command -eq 'Download' -and -not $Options.dry) {
            $trackedJobs=@($script:State.jobs.Values|Where-Object {$_.downloads.Count})
            Sync-RequestedDownloads $Connection $trackedJobs
        }
        return @{dryRun=$Options.dry;scope=$(if($Options.folder){$Options.folder}else{'云盘根目录，排除已下载及已开始下载'});resources=$results;summary=(Get-Summary)}
    }
    throw '未知手动命令'
}

if($script:ManualOptions.library){return}
Read-State
if($script:ManualOptions.command -eq 'Requests') {
    $requests=@(Get-ChildItem -LiteralPath $script:RecordDir -Filter '请求模块-*-v1.json' -ErrorAction SilentlyContinue|ForEach-Object {Get-Content -Raw -Encoding UTF8 -LiteralPath $_.FullName|ConvertFrom-Json})
    @{requests=$requests;summary=(Get-Summary)}|ConvertTo-Json -Depth 20;return
}
if($script:ManualOptions.limit -le 0 -or $script:ManualOptions.limit -gt 100){throw 'zero 进度门槛必须大于0且不超过100'}
$mutex=[Threading.Mutex]::new($false,'Global\ThunderCloudTransferV1');$owned=$false;$connection=$null
try {
    try{$owned=$mutex.WaitOne(30000)}catch [Threading.AbandonedMutexException]{$owned=$true}
    if(-not $owned){
        if($script:ManualOptions.command -eq 'Due'){$r=Read-Request $script:ManualOptions.request;Register-Once $r ((Get-Now)+60);return}
        if($script:ManualOptions.command -eq 'CleanRecycle'){$r=Read-Request $script:ManualOptions.request;Register-RecycleCleanup $r ((Get-Now)+60);return}
        throw '其他转存命令正在执行，请稍后重试；不会并发删除任务'
    }
    # 锁内重新读取，防止等待期间读到旧状态。
    Read-State
    $connection=Open-ThunderConnection
    $result=Invoke-ManualCommand $connection $script:ManualOptions
    $result|ConvertTo-Json -Depth 22
} catch {
    if($script:ManualOptions.command -eq 'Due'){
        $r=Read-Request $script:ManualOptions.request;$r.stage='error';$r.error=$_.Exception.Message;Save-Request $r
    }
    if($script:ManualOptions.command -eq 'CleanRecycle'){
        $r=Read-Request $script:ManualOptions.request
        if((Get-Now) -lt $r.cleanupDeadline){Register-RecycleCleanup $r ((Get-Now)+300)}else{$r.cleanupStage='review';Save-Request $r}
    }
    Write-Event '手动命令异常' @{command=$script:ManualOptions.command;error=$_.Exception.Message}
    throw
}finally{Close-ThunderConnection $connection;if($owned){$mutex.ReleaseMutex()};$mutex.Dispose()}
