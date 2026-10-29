[CmdletBinding()]
param(
    [ValidateSet('Inspect','Once','Watch','Transfer','Status')][string]$Mode='Inspect',
    [string]$WorkDir=$PSScriptRoot,
    [long]$TaskId=0,
    [switch]$LibraryOnly
)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding=[Text.Encoding]::UTF8
. (Join-Path $PSScriptRoot 'ipc.ps1')
$script:Config=Get-Content -Raw -Encoding UTF8 (Join-Path $PSScriptRoot 'rules.json') | ConvertFrom-Json
$script:RecordDir=Join-Path $WorkDir '运行记录'
$script:StatePath=Join-Path $script:RecordDir '状态模块-转存记录-v1.json'
$script:State=$null

function ConvertTo-Map($Value) {
    if ($null -eq $Value) { return $null }
    if ($Value -is [System.Management.Automation.PSCustomObject]) {
        $map=@{}; foreach($property in $Value.PSObject.Properties) { $map[$property.Name]=ConvertTo-Map $property.Value }; return $map
    }
    if ($Value -is [System.Collections.IDictionary]) { return $Value }
    if ($Value -is [Array]) { $items=@(); foreach($item in $Value) { $items+=,(ConvertTo-Map $item) }; return ,$items }
    return $Value
}
function Get-Now { return [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }
function Save-State {
    New-Item -ItemType Directory -Path $script:RecordDir -Force | Out-Null
    $temporary=$script:StatePath+'.tmp'
    $script:State.updatedAt=[DateTimeOffset]::Now.ToString('o')
    [IO.File]::WriteAllText($temporary,($script:State|ConvertTo-Json -Depth 60),[Text.UTF8Encoding]::new($true))
    if (Test-Path -LiteralPath $script:StatePath) { [IO.File]::Replace($temporary,$script:StatePath,$script:StatePath+'.bak') }
    else { [IO.File]::Move($temporary,$script:StatePath) }
}
function Read-State {
    if (Test-Path -LiteralPath $script:StatePath) {
        $script:State=ConvertTo-Map (Get-Content -Raw -Encoding UTF8 -LiteralPath $script:StatePath | ConvertFrom-Json)
        if ($script:State.schema -ne 1 -or $null -eq $script:State.jobs) { throw '转存记录格式不正确，已停止操作' }
    } else { $script:State=@{schema=1;rootFolderId='';rootFolderName='';ownerId='';jobs=@{};observations=@{};updatedAt='';lastError=''} }
}
function Write-Event([string]$Event,$Details) {
    New-Item -ItemType Directory -Path $script:RecordDir -Force | Out-Null
    $log=Join-Path $script:RecordDir '日志模块-转存操作-v1.jsonl'
    if ((Test-Path -LiteralPath $log) -and (Get-Item -LiteralPath $log).Length -gt 10MB) { Move-Item -LiteralPath $log -Destination ($log+'.previous') -Force }
    @{time=[DateTimeOffset]::Now.ToString('o');event=$Event;details=$Details} | ConvertTo-Json -Compress -Depth 15 | Add-Content -LiteralPath $log -Encoding UTF8
}
function Test-ConnectionFault($Failure) {
    return ([string]$Failure.Exception.Message -match '响应超时|连接已关闭|管道断开|读取流|无法写入|信号灯超时|Stream does not support|pipe is being closed')
}
function Get-LocalTask($Connection,[long]$Id) {
    $t=(Invoke-ThunderApi $Connection 'TaskManagerTaskGetTaskBase' @($Id)).data
    if ($t.taskId -eq $Id -and $t.recycleTime -eq 0) { return $t }
    return $null
}
function Get-ActiveTasks($Connection) {
    $ids=(Invoke-ThunderApi $Connection 'CategoryManagerCategoryGetDownloadingTaskIds' @(-1)).data
    if ($null -eq $ids) {throw '读取活动任务失败'}
    foreach($id in $ids) { $t=Get-LocalTask $Connection $id; if($null -ne $t) {$t} }
}
function Test-Candidate($Task) {
    if ($null -eq $Task -or $Task.origin -eq 'cloud_bridge_v1') {return $false}
    foreach($key in @('taskId','taskType','taskStatus','downloadSize','downloadSpeed','fileSize','recycleTime')) {
        if ($null -eq $Task.$key -or $Task.$key -isnot [ValueType]) {return $false}
    }
    return ($Task.taskId -gt 0 -and $Task.taskType -in @(1,2,5) -and $Task.taskStatus -eq 5 -and
        $Task.recycleTime -eq 0 -and $Task.isPanTask -ne $true -and $Task.fileSize -gt 0 -and
        $Task.downloadSize -ge 0 -and $Task.downloadSpeed -le $script:Config.maxSpeedBytes -and
        100.0*[double]$Task.downloadSize/[double]$Task.fileSize -lt $script:Config.maxProgressPercent -and
        -not [string]::IsNullOrWhiteSpace($Task.savePath))
}
function Get-Children($Connection,[string]$ParentId) {
    $page=''; $seen=@{}
    do {
        $r=Invoke-PanApi $Connection 'getFiles' @(@{params=@{parent_id=$ParentId;page_token=$page;limit=200}})
        if ($null -eq $r.files) {throw '云盘未返回目录列表'}
        foreach($file in $r.files) {if(-not $file.trashed){$file}}
        $page=[string]$r.next_page_token
        if ($page -and $seen.ContainsKey($page)) {throw '云盘分页标记重复'}
        if ($page) {$seen[$page]=$true}
    } while($page)
}
function Get-OwnedFolder($Connection) {
    if ($script:State.rootFolderId) {
        $f=Invoke-PanApi $Connection 'getFileInfo' @($script:State.rootFolderId,@{params=@{space='';usage='DISPLAY'}})
        if ($f.kind -ne 'drive#folder' -or $f.trashed -or $f.user_id -ne $script:State.ownerId) {throw '专属云盘目录或登录账号发生变化，停止自动操作'}
        return $f.id
    }
    # 目录名含随机后缀，恢复时只认自己记录的目录，不接管同名用户目录。
    if (-not $script:State.rootFolderName) {
        $script:State.rootFolderName=$script:Config.cloudFolderName+'-'+[DateTime]::Now.ToString('yyyyMMdd')+'-'+[Guid]::NewGuid().ToString('N').Substring(0,8)
        Save-State
    }
    $matches=@(Get-Children $Connection '' | Where-Object {$_.name -eq $script:State.rootFolderName -and $_.kind -eq 'drive#folder'})
    if($matches.Count -gt 1){throw '专属目录存在多项匹配，需要人工核对'}
    if($matches.Count -eq 1){$f=$matches[0]}
    else{$f=(Invoke-PanApi $Connection 'createFolder' @('',$script:State.rootFolderName,@{params=@{}})).file}
    if(-not $f.id -or -not $f.user_id){throw '创建专属云盘目录未返回有效信息'}
    $script:State.rootFolderId=$f.id; $script:State.ownerId=$f.user_id; Save-State
    return $f.id
}
function Get-SafeSegment([string]$Name) {
    if ([string]::IsNullOrWhiteSpace($Name) -or $Name -in @('.','..') -or $Name -match '[\\/:*?"<>|\x00-\x1f]' -or $Name -match '[ .]$' -or $Name -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)') {throw "文件名不适合安全保存：$Name"}
    return $Name
}
function Get-ReadyLeaves($Connection,[string]$FolderId,[string]$Relative='', [int]$Depth=0) {
    if($Depth -gt 20){throw '云盘目录层级超过20层'}
    foreach($f in @(Get-Children $Connection $FolderId)) {
        if($f.user_id -ne $script:State.ownerId -or $f.parent_id -ne $FolderId){throw '云盘文件归属不匹配'}
        $name=Get-SafeSegment $f.name
        $path=if($Relative){$Relative+'\'+$name}else{$name}
        if($f.kind -eq 'drive#folder'){Get-ReadyLeaves $Connection $f.id $path ($Depth+1)}
        elseif($f.kind -eq 'drive#file' -and $f.phase -eq 'PHASE_TYPE_COMPLETE') { @{file=$f;relative=$path} }
    }
}
function Find-OriginalFile($Job,[string]$Relative,$File) {
    if(-not $Job.btFiles.Count){return @{download=$true;downloadSize=$Job.original.downloadSize;fileSize=$Job.original.fileSize}}
    # original_file_index 在该版本云盘里均返回0，使用路径及大小核对 BT 选择，重名歧义留给人工。
    $matches=@($Job.btFiles | Where-Object {
        $name=([string]$_.fileName).Replace('/','\')
        $path=([string]$_.filePath).Replace('/','\').Trim('\')
        $full=if($path){$path+'\'+$name}else{$name}
        ([long]$_.fileSize -eq [long]$File.size) -and ($Relative -eq $full -or $Relative.EndsWith('\'+$full))
    })
    if($matches.Count -ne 1){return $null}
    return $matches[0]
}
function New-Job($Connection,$Task,[bool]$Explicit=$false) {
    $key=[string]$Task.taskId
    if($script:State.jobs.ContainsKey($key)){return $script:State.jobs[$key]}
    if($Task.isPanTask -or $Task.taskStatus -ne 5){throw '仅可转存正在下载的普通本地任务'}
    if(-not $Explicit -and -not (Test-Candidate $Task)){throw '任务已不符合慢速规则'}
    $url=[string]$Task.url; $bt=@()
    if($Task.taskType -in @(2,5)) {
        $bt=@((Invoke-ThunderApi $Connection 'TaskManagerTaskBtTaskGetBtFileInfos' @($Task.taskId)).data)
        if(-not $bt.Count -or -not @($bt|Where-Object {$_.download -eq $true}).Count){throw '无法保存BT文件选择，保留原任务'}
        if($url -notmatch '^(magnet:\?|https?://|ed2k://|thunder://)') {
            $hash=(Invoke-ThunderApi $Connection 'TaskManagerTaskBtTaskGetInfoHash' @($Task.taskId)).data
            if([string]$hash -notmatch '^[a-fA-F0-9]{40}$'){throw '找不到可恢复的磁力链接，保留原任务'}
            $url='magnet:?xt=urn:btih:'+ $hash
        }
    }
    if($url -notmatch '^(magnet:\?|https?://|ed2k://|thunder://)'){throw '原链接无法转存，保留原任务'}
    $job=@{id=$key;name=$Task.taskName;original=ConvertTo-Map $Task;url=$url;btFiles=@($bt|ForEach-Object {ConvertTo-Map $_});
        stage='prepared';createdAt=Get-Now;folderName=('任务-'+$key);folderId='';cacheTaskId='';cacheIntent=$false;
        deadline=0;downloads=@{};error='';manualRemaining=$true;deleteIntent=$false;originalDeleted=$false}
    # 保存原始链接、完整任务和BT勾选信息后，才允许进行任何删除。
    $script:State.jobs[$key]=$job;Save-State;Write-Event '已保存原任务' @{id=$key;name=$Task.taskName}
    return $job
}
function Start-Job($Connection,$Job) {
    $root=Get-OwnedFolder $Connection
    if(-not $Job.folderId) {
        $parent=$root
        if($Job.batchFolderName) {
            if(-not $script:State.batches){$script:State.batches=@{}}
            if(-not $script:State.batches.ContainsKey([string]$Job.requestId)) {
                $script:State.batches[[string]$Job.requestId]=@{name=$Job.batchFolderName;folderId='';createdAt=$Job.createdAt};Save-State
            }
            $batch=$script:State.batches[[string]$Job.requestId]
            if($batch.name -ne $Job.batchFolderName){throw '批次目录名称与持久记录不一致'}
            if($batch.folderId) {
                $f=Invoke-PanApi $Connection 'getFileInfo' @($batch.folderId,@{params=@{space='';usage='DISPLAY'}})
                if($f.id -ne $batch.folderId -or $f.kind -ne 'drive#folder' -or $f.user_id -ne $script:State.ownerId -or $f.parent_id -ne $root -or $f.trashed){throw '记录中的批次目录已变化'}
            } else {
                $batch.folderId=Get-CheckedFolder $Connection $root $batch.name;Save-State
            }
            $parent=[string]$batch.folderId;$Job.batchFolderId=$parent;Save-State
        }
        $matches=@(Get-Children $Connection $parent | Where-Object {$_.name -eq $Job.folderName -and $_.kind -eq 'drive#folder'})
        if($matches.Count -gt 1){throw '任务目录重名，需要人工核对'}
        $f=if($matches.Count -eq 1){$matches[0]}else{(Invoke-PanApi $Connection 'createFolder' @($parent,$Job.folderName,@{params=@{}})).file}
        if(-not $f.id){throw '未返回任务目录ID'}
        $Job.folderId=$f.id;Save-State
    }
    if(-not $Job.originalDeleted -and $Job.source -eq 'recycle') {
        $fresh=(Invoke-ThunderApi $Connection 'TaskManagerTaskGetTaskBase' @([long]$Job.id)).data
        if($fresh.taskId -ne [long]$Job.id -or $fresh.recycleTime -le 0 -or $fresh.url -ne $Job.original.url -or $fresh.taskName -ne $Job.name){throw '回收站任务身份已变化，停止转存'}
        # 提交阶段仅保存原链接；新请求确认云缓存完成后再单条清理回收站。
        $Job.originalDeleted=$true;$Job.stage='deleted';Save-State
    }
    if(-not $Job.originalDeleted) {
        $fresh=Get-LocalTask $Connection ([long]$Job.id)
        $activeReply=(Invoke-ThunderApi $Connection 'CategoryManagerCategoryGetDownloadingTaskIds' @(-1)).data
        if($null -eq $activeReply -or [long]$Job.id -notin @($activeReply)){throw '原任务已离开当前下载列表，停止转存'}
        if($fresh) {
            if($fresh.url -ne $Job.original.url -or $fresh.taskName -ne $Job.name -or $fresh.savePath -ne $Job.original.savePath){throw '原任务身份已变化，停止删除'}
            $eligible=if($Job.manualMode){Test-ManualCandidate $fresh ([double]$Job.progressLimit)}else{Test-Candidate $fresh}
            if(-not $Job.deleteIntent -and -not $eligible){throw '原任务速度或进度已变化，取消本次转存'}
            $Job.deleteIntent=$true;Save-State
            $r=(Invoke-ThunderApi $Connection 'TaskManagerTaskDeleteTask' @([long]$Job.id,[bool]$script:Config.deleteOriginalFiles)).data
            if($null -eq $r -or $r -ne 0){throw "删除原任务失败，返回码 $r"}
            if(Get-LocalTask $Connection ([long]$Job.id)){throw '删除后原任务仍在活动列表中'}
        } elseif(-not $Job.deleteIntent){throw '原任务在删除前已消失，需人工核对'}
        $Job.originalDeleted=$true;$Job.stage='deleted';Save-State;Write-Event '已删除原任务' @{id=$Job.id;deleteFiles=$script:Config.deleteOriginalFiles}
    }
    if(-not $Job.cacheIntent) {
        $Job.cacheIntent=$true;$Job.stage='submitting';$Job.deadline=(Get-Now)+[int]$script:Config.instantWindowSeconds;Save-State
        $r=Invoke-PanApi $Connection 'createFile' @(@{params=@{upload_type='UPLOAD_TYPE_URL';kind='drive#file';parent_id=$Job.folderId;name=$Job.name;hash='';size='0';space='';url=@{url=$Job.url};params=@{require_links='false'}}})
        if(-not $r.task.id -and -not $r.file.id){throw '提交缓存没有返回文件或任务ID，禁止盲目重试'}
        $Job.cacheTaskId=[string]$r.task.id;$Job.cacheFileId=[string]$r.file.id;$Job.cacheAcknowledged=$true;$Job.stage='caching';
        if($Job.manualMode){$Job.downloadNotBefore=(Get-Now)+[int]$Job.waitSeconds}
        Save-State
        Write-Event '已提交云缓存' @{id=$Job.id;cacheTaskId=$Job.cacheTaskId;folderId=$Job.folderId}
    } elseif($Job.stage -eq 'submitting') {
        # 网络超时时不重复提交；只核对专属目录中已经产生的结果。
        $Job.stage='caching';$Job.error='上次提交响应不明，按现有云盘内容核对；不重复提交';Save-State
    }
}
function Find-RecoveryTask($Connection,$Record) {
    # 以云盘文件ID查询原生下载任务；响应丢失时禁止再次盲目创建。
    $found=(Invoke-ThunderApi $Connection 'TaskManagerFindPanTaskByFileId' @($Record.fileId)).data
    if($found -and [long]$found -gt 0){return [long]$found}
    return 0
}
function Get-CheckedFolder($Connection,[string]$Parent,[string]$Name) {
    $null=Get-SafeSegment $Name
    $matches=@(Get-Children $Connection $Parent | Where-Object {$_.name -eq $Name -and $_.kind -eq 'drive#folder'})
    if($matches.Count -gt 1){throw '云盘目标目录存在重名，保留文件等待核对'}
    $f=if($matches.Count){$matches[0]}else{(Invoke-PanApi $Connection 'createFolder' @($Parent,$Name,@{params=@{}})).file}
    if(-not $f.id -or $f.user_id -ne $script:State.ownerId -or $f.parent_id -ne $Parent -or $f.trashed){throw '目标目录归属不匹配'}
    return [string]$f.id
}
function Assert-OwnedDescendant($Connection,[string]$Folder,[string]$Ancestor) {
    for($depth=0;$depth -le 22;$depth++) {
        if($Folder -eq $Ancestor){return}
        if(-not $Folder){break}
        $f=Invoke-PanApi $Connection 'getFileInfo' @($Folder,@{params=@{space='';usage='DISPLAY'}})
        if($f.kind -ne 'drive#folder' -or $f.trashed -or $f.user_id -ne $script:State.ownerId){break}
        $Folder=[string]$f.parent_id
    }
    throw '云盘文件或目标目录已离开本任务的专属目录，保留给人工核对'
}
function Get-MoveTarget($Connection,$Job,$Record) {
    $root=Get-OwnedFolder $Connection
    $downloadedArchive=($Job.manualMode -or $Record.archiveMode -eq 'downloaded')
    if($downloadedArchive) {
        if(-not $script:State.downloadedFolderId){$script:State.downloadedFolderId=Get-CheckedFolder $Connection $root '已下载';Save-State}
        Assert-OwnedDescendant $Connection $script:State.downloadedFolderId $root
        $parent=$script:State.downloadedFolderId
    } else {
    if(-not $script:State.startedFolderId) {
        $script:State.startedFolderId=Get-CheckedFolder $Connection $root '已开始下载';Save-State
    }
    Assert-OwnedDescendant $Connection $script:State.startedFolderId $root
    $parent=$script:State.startedFolderId
    }
    $relativeDir=Split-Path -Path $Record.relative -Parent
    $parts=@()
    if($Job.batchFolderName){$parts+=,$Job.batchFolderName}
    $parts+=,$Job.folderName
    if($relativeDir){$parts+=@($relativeDir.Split('\'))}
    if(-not $Job.moveFolders){$Job.moveFolders=@{}}
    $path=if($downloadedArchive){'\已下载'}else{''}
    foreach($part in $parts) {
        $path+='\'+(Get-SafeSegment $part)
        if($Job.moveFolders.ContainsKey($path)) {
            $f=Invoke-PanApi $Connection 'getFileInfo' @($Job.moveFolders[$path],@{params=@{space='';usage='DISPLAY'}})
            if($f.kind -ne 'drive#folder' -or $f.user_id -ne $script:State.ownerId -or $f.parent_id -ne $parent -or $f.trashed){throw '记录中的移动目标目录已变化'}
            $parent=[string]$f.id
        } else {
            $parent=Get-CheckedFolder $Connection $parent $part
            $Job.moveFolders[$path]=$parent;Save-State
        }
    }
    if($Record.destinationId -and $Record.destinationId -ne $parent){throw '已保存的移动目标与本任务目录不一致'}
    $Record.destinationId=$parent;Save-State
    return $parent
}
function Move-StartedFile($Connection,$Job,$Record) {
    $target=Get-MoveTarget $Connection $Job $Record
    $info=Invoke-PanApi $Connection 'getFileInfo' @($Record.fileId,@{params=@{space='';usage='DISPLAY'}})
    if($info.id -ne $Record.fileId -or $info.kind -ne 'drive#file' -or $info.user_id -ne $script:State.ownerId -or $info.name -ne $Record.name -or [long]$info.size -ne [long]$Record.size -or $info.trashed){throw '待移动云盘文件身份不匹配'}
    if($info.parent_id -eq $target) {
        $Record.stage='moved';$Record.movedAt=Get-Now;$Record.error='';Save-State
        Write-Event '已移入下载归档目录' @{job=$Job.id;fileId=$Record.fileId;taskId=$Record.taskId;name=$Record.name;destinationId=$target}
        return
    }
    if($info.parent_id -ne $Record.parentId){throw '文件已被移动到其他位置，保留给人工核对'}
    Assert-OwnedDescendant $Connection $Record.parentId $Job.folderId
    if($Record.moveIntent) {
        # 异步移动或响应丢失时只核对实际位置，不能重新创建下载。
        if((Get-Now)-$Record.moveRequestedAt -gt 300){$Record.stage='review';$Record.error='移动请求超过5分钟未确认，请人工检查云盘位置';Save-State}
        return
    }
    $Record.moveIntent=$true;$Record.moveRequestedAt=Get-Now;Save-State
    $result=Invoke-PanApi $Connection 'batchMoveFiles' @(@{params=@{ids=@($Record.fileId);to=@{parent_id=$target;space=''};space=''}})
    $Record.moveTaskId=[string]$result.task_id;Save-State
    # 后续轮次以文件的实际父目录确认异步移动结果。
}
function Update-Download($Connection,$Job,$Record) {
    if($Record.stage -in @('moved','external','unselected','ambiguous','failed','review')){return}
    if($Record.stage -eq 'trashed'){throw '发现旧删除实验记录，须先恢复云盘文件并迁移记录'}
    if($Record.stage -eq 'alreadyLocal') {
        if(-not (Test-Path -LiteralPath $Record.savePath -PathType Leaf) -or (Get-Item -LiteralPath $Record.savePath).Length -ne [long]$Record.size){$Record.stage='review';$Record.error='原已完成文件不再存在或大小变化，保留云盘文件';Save-State;return}
        $Record.alreadyLocal=$true
        Move-StartedFile $Connection $Job $Record;return
    }
    if(-not $Record.taskId -and $Record.intent) {
        $found=Find-RecoveryTask $Connection $Record
        if($found){$Record.taskId=$found;$Record.stage='created';Save-State}
        else{$Record.stage='review';$Record.error='创建响应不明且未找到任务，请人工核对，禁止重复创建';Save-State;return}
    }
    if(-not $Record.taskId){return}
    $t=Get-LocalTask $Connection ([long]$Record.taskId)
    if(-not $t){$Record.stage='review';$Record.error='本地任务已不存在，保留云盘文件';Save-State;return}
    if(-not $t.isPanTask -or $t.taskName -ne $Record.name -or $t.savePath.TrimEnd('\') -ne $Record.savePath.TrimEnd('\')) {
        $Record.stage='review';$Record.error='本地任务类型、路径或名称不匹配，保留云盘文件';Save-State;return
    }
    # 实际收到数据才确认启动；排队、零进度或失败时保留原云盘位置。
    $started=($t.taskStatus -eq 8 -or ($t.taskStatus -eq 5 -and ($t.downloadSpeed -gt 0 -or $t.downloadSize -gt $Record.initialBytes)))
    if(-not $started){if($t.taskStatus -eq 9){$Record.error='本地下载失败，保留云盘文件';Save-State};return}
    if(-not $Record.startedAt){$Record.startedAt=Get-Now}
    $Record.stage='started';$Record.error='';Save-State
    Move-StartedFile $Connection $Job $Record
}
function Process-ReadyFile($Connection,$Job,$Leaf) {
    $f=$Leaf.file;$key=[string]$f.id
    if($Job.downloads.ContainsKey($key)){return}
    $original=Find-OriginalFile $Job $Leaf.relative $f
    $record=@{fileId=$key;parentId=$f.parent_id;name=$f.name;relative=$Leaf.relative;size=$f.size;stage='ready';taskId=0;intent=$false;moveIntent=$false;destinationId='';initialBytes=0;startedAt=0;movedAt=0;savePath='';error='';transport='native';archiveMode=$(if($Job.manualMode -or $Job.newArchiveMode){'downloaded'}else{'started'})}
    $Job.downloads[$key]=$record
    if($null -eq $original){$record.stage='ambiguous';$record.error='无法唯一对应原BT文件，保留给人工';Save-State;return}
    if(-not $original.download){$record.stage='unselected';Save-State;return}
    # 原任务已经完成的文件若在磁盘上存在且大小一致，则沿用原文件，不重复下载。
    if($Job.btFiles.Count -and [long]$original.downloadSize -eq [long]$original.fileSize) {
        $existing=Join-Path (Join-Path $Job.original.savePath $Job.name) ($Leaf.relative.Substring($Leaf.relative.IndexOf('\')+1))
        if((Test-Path -LiteralPath $existing -PathType Leaf) -and (Get-Item -LiteralPath $existing).Length -eq [long]$f.size){$record.stage='alreadyLocal';$record.savePath=$existing;Save-State;return}
    }
    $save=if($Job.outputRoot){Join-Path $Job.outputRoot $Job.folderName}else{Join-Path (Join-Path $Job.original.savePath $script:Config.localSubdirectory) $Job.folderName}
    $relativeDir=Split-Path -Path $Leaf.relative -Parent
    if($relativeDir){$save=Join-Path $save $relativeDir}
    $record.savePath=$save
    New-Item -ItemType Directory -Path $save -Force | Out-Null
    $found=Find-RecoveryTask $Connection $record
    if($found){$record.taskId=$found;$record.stage='created';Save-State;Update-Download $Connection $Job $record;return}
    $record.intent=$true;Save-State
    $result=Invoke-PanApi $Connection 'batchCreateDownloadTaskWithFiles' @(@($f),$save,@{})
    $ids=@($result.taskIds | Where-Object {$_ -and [long]$_ -gt 0})
    if($ids.Count -ne 1) {
        $found=Find-RecoveryTask $Connection $record
        if($found){$ids=@($found)}else{$record.stage='review';$record.error='未返回唯一原生云盘下载任务ID，保留文件等待人工核对';Save-State;return}
    }
    $record.taskId=[long]$ids[0];$record.stage='created';Save-State
    Write-Event '已创建本地云盘下载' @{job=$Job.id;fileId=$key;taskId=$record.taskId;savePath=$save}
}
function Resume-Job($Connection,$Job) {
    if($Job.stage -eq 'review'){return}
    if($Job.stage -in @('prepared','deleted','submitting')){Start-Job $Connection $Job}
    foreach($record in @($Job.downloads.Values)){
        try {Update-Download $Connection $Job $record}
        catch {$record.error=$_.Exception.Message;Write-Event '下载或移动待重试' @{job=$Job.id;fileId=$record.fileId;error=$record.error};Save-State;if(Test-ConnectionFault $_){throw}}
    }
    if($Job.manualMode){return}
    if($Job.stage -ne 'caching'){return}
    if((Get-Now) -le $Job.deadline) {
        foreach($leaf in @(Get-ReadyLeaves $Connection $Job.folderId)) {
            if((Get-Now) -gt $Job.deadline){break}
            try { Process-ReadyFile $Connection $Job $leaf }
            catch {
                $failure=$_;$record=$Job.downloads[[string]$leaf.file.id]
                if($record){$record.error=$failure.Exception.Message;if(-not $record.intent){$record.stage='review'}}
                Write-Event '单文件处理异常' @{job=$Job.id;fileId=$leaf.file.id;error=$failure.Exception.Message}
                $Job.error=$failure.Exception.Message;Save-State;if(Test-ConnectionFault $failure){throw}
            }
        }
    } else {
        $Job.stage='manual';Save-State;Write-Event '秒缓存窗口结束' @{job=$Job.id;folderId=$Job.folderId;message='后续缓存留给人工，不再自动创建下载'}
    }
}
function Get-Summary {
    $jobs=@($script:State.jobs.Values|ForEach-Object {
        @{id=$_.id;name=$_.name;stage=$_.stage;error=$_.error;folderId=$_.folderId;originalDeleted=$_.originalDeleted;
          localDownloads=@($_.downloads.Values|Where-Object {$_.taskId -gt 0}).Count;
          cloudMoved=@($_.downloads.Values|Where-Object {$_.stage -eq 'moved'}).Count;
          review=@($_.downloads.Values|Where-Object {$_.stage -eq 'review' -or $_.error}).Count;
          alreadyLocal=@($_.downloads.Values|Where-Object {$_.stage -eq 'alreadyLocal' -or $_.alreadyLocal}).Count;
          waiting=@($_.downloads.Values|Where-Object {$_.stage -in @('ready','created','started')}).Count}
    })
    return @{time=[DateTimeOffset]::Now.ToString('o');rootFolderName=$script:State.rootFolderName;rootFolderId=$script:State.rootFolderId;
        updatedAt=$script:State.updatedAt;lastSuccessfulRoundAt=$script:State.lastSuccessfulRoundAt;rules=$script:Config;observations=@($script:State.observations.Values);jobs=$jobs;lastError=$script:State.lastError}
}
function Invoke-Round($Connection) {
    $active=@(Get-ActiveTasks $Connection)
    foreach($job in @($script:State.jobs.Values)) {
        try {Resume-Job $Connection $job}
        catch {$job.error=$_.Exception.Message;if($job.stage -in @('prepared','deleted')){$job.stage='review'};Write-Event '任务处理待核对' @{job=$job.id;error=$job.error};Save-State;if(Test-ConnectionFault $_){throw}}
    }
    $next=@{};$count=0;$now=Get-Now
    foreach($t in $active) {
        $key=[string]$t.taskId
        if($script:State.jobs.ContainsKey($key) -or -not (Test-Candidate $t)){continue}
        $old=$script:State.observations[$key];$first=$now
        if($old -and $old.url -eq $t.url -and $old.savePath -eq $t.savePath -and $old.name -eq $t.taskName -and $old.downloadSize -eq $t.downloadSize -and ($now-$old.last) -le [Math]::Max(60,3*$script:Config.pollSeconds)){$first=$old.first}
        $ob=@{id=$key;name=$t.taskName;first=$first;last=$now;elapsed=$now-$first;url=$t.url;savePath=$t.savePath;downloadSize=$t.downloadSize}
        $next[$key]=$ob
        if($ob.elapsed -ge $script:Config.stallSeconds -and $count -lt $script:Config.maxTransfersPerRound) {
            $fresh=Get-LocalTask $Connection $t.taskId
            if(-not (Test-Candidate $fresh) -or $fresh.downloadSize -ne $t.downloadSize){continue}
            $job=$null
            try {$job=New-Job $Connection $fresh;Start-Job $Connection $job;Resume-Job $Connection $job}
            catch {if($job){$job.error=$_.Exception.Message;if($job.stage -in @('prepared','deleted')){$job.stage='review'}};Write-Event '转存待人工核对' @{id=$key;error=$_.Exception.Message};Save-State;if(Test-ConnectionFault $_){throw}}
            $next.Remove($key);$count++
        }
    }
    $script:State.observations=$next;$script:State.lastError='';$script:State.lastSuccessfulRoundAt=[DateTimeOffset]::Now.ToString('o');Save-State
}
if($LibraryOnly){return}
Read-State
if($Mode -eq 'Status'){Get-Summary|ConvertTo-Json -Depth 15;return}
if($Mode -eq 'Inspect') {
    $connection=$null
    try {
        $connection=Open-ThunderConnection
        $tasks=@(Get-ActiveTasks $connection)
        $null=Invoke-PanApi $connection 'getFiles' @(@{params=@{parent_id='';limit=20}})
        @{activeTasks=$tasks.Count;candidates=@($tasks|Where-Object {Test-Candidate $_}|Select-Object taskId,taskName,downloadSpeed,downloadSize,fileSize,savePath);rules=$script:Config;cloudConnected=$true}|ConvertTo-Json -Depth 10
    }finally{Close-ThunderConnection $connection}
    return
}
if($script:Config.deleteOriginalFiles -ne $false){throw '本版本必须保留原任务已有本地文件'}
if($script:Config.stallSeconds -lt 600 -or $script:Config.pollSeconds -lt 5 -or $script:Config.maxTransfersPerRound -lt 1 -or $script:Config.instantWindowSeconds -lt 5 -or $script:Config.instantWindowSeconds -gt 120){throw '规则超出安全范围：观察至少600秒，采样至少5秒，秒缓存窗口5至120秒'}
$mutex=[Threading.Mutex]::new($false,'Global\ThunderCloudTransferV1');$owned=$false
try {
    try{$owned=$mutex.WaitOne(0)}catch [Threading.AbandonedMutexException]{$owned=$true}
    if(-not $owned){throw '转存监控正在运行；单次操作前请先停止监控'}
    do {
        $connection=$null
        try {
            $connection=Open-ThunderConnection
            if($Mode -eq 'Transfer') {
                $key=[string]$TaskId
                $job=if($script:State.jobs.ContainsKey($key)){$script:State.jobs[$key]}else{New-Job $connection (Get-LocalTask $connection $TaskId) $true}
                if($job.stage -eq 'prepared'){Start-Job $connection $job}
                $until=[Math]::Max((Get-Now),$job.deadline)+[int]$script:Config.startGraceSeconds
                do {Resume-Job $connection $job;if((Get-Now) -ge $until){break};Start-Sleep -Seconds 3}while($true)
            } else {Invoke-Round $connection}
        } catch {
            $script:State.observations=@{};$script:State.lastError=$_.Exception.Message;Save-State
            Write-Event '本轮异常' @{error=$_.Exception.Message}
            if($Mode -ne 'Watch'){throw}
        } finally {Close-ThunderConnection $connection}
        if($Mode -ne 'Watch'){Get-Summary|ConvertTo-Json -Depth 15;break}
        Start-Sleep -Seconds $script:Config.pollSeconds
        # 所有循环之间都从磁盘读取，可在停机后恢复提交意图和下载状态。
        Read-State
    }while($true)
}finally{if($owned){$mutex.ReleaseMutex()};$mutex.Dispose()}
