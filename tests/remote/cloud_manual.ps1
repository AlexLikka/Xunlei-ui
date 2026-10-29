[CmdletBinding()]
param([string]$ModuleDir=$PSScriptRoot)
$ErrorActionPreference='Stop'
$testDir=Join-Path $env:TEMP ('迅雷手动测试-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testDir -Force|Out-Null
try {
    . (Join-Path $ModuleDir 'manual.ps1') -WorkDir $testDir -LibraryOnly
    $nativeProcess=${function:Process-ReadyFile}
    $nativeCheckedFolder=${function:Get-CheckedFolder}
    $script:passed=@();$script:clock=1000
    function Assert-Test([bool]$Value,[string]$Name){if(-not $Value){throw ('测试失败：'+$Name)};$script:passed+=,$Name}
    function Get-Now{return $script:clock}
    $script:State=@{schema=1;ownerId='owner';rootFolderId='root';jobs=@{};observations=@{};lastError=''}
    $script:Config.startGraceSeconds=0
    $candidate=@{taskId=[long]99;taskName='规则验证';taskType=2;taskStatus=5;downloadSize=[long]19;downloadSpeed=[long]0;fileSize=[long]100;recycleTime=[long]0;isPanTask=$false;savePath='D:\测试';url='magnet:?xt=urn:btih:123';origin=''}
    Assert-Test (Test-ManualCandidate $candidate 20) 'zero20 包含19%零速下载'
    $candidate.downloadSize=[long]20
    Assert-Test (-not (Test-ManualCandidate $candidate 20)) 'zero20 排除恰好20%'
    $candidate.downloadSize=[long]0;$candidate.isPanTask=$true
    Assert-Test (-not (Test-ManualCandidate $candidate 20)) '云盘下载不能再次转存'
    $candidate.isPanTask=$false;$candidate.taskStatus=7
    Assert-Test (-not (Test-ManualCandidate $candidate 20)) '暂停任务不纳入zero20'
    $candidate.taskStatus=5;$candidate.downloadSpeed=[long]1
    Assert-Test (-not (Test-ManualCandidate $candidate 20)) '非零速度不纳入zero20'
    $candidate.downloadSpeed=[long]0
    $caught=$false;try{Assert-OutputPath '..\下载'}catch{$caught=$true}
    Assert-Test $caught '下载目录拒绝相对路径'
    Assert-OutputPath 'D:\电影 下载'
    Assert-Test $true 'Windows路径允许中文及空格'
    $script:bt=@(@{fileName='一.mkv';filePath='';fileSize=100;downloadSize=0;download=$true},@{fileName='二.mkv';filePath='';fileSize=200;downloadSize=0;download=$true})
    $script:leaf1=@{file=@{id='one';name='一.mkv';parent_id='job';user_id='owner';size=100;kind='drive#file';phase='PHASE_TYPE_COMPLETE'};relative='资源\一.mkv';pending=$false}
    $script:leaf2=@{file=@{id='two';name='二.mkv';parent_id='job';user_id='owner';size=200;kind='drive#file';phase='PHASE_TYPE_COMPLETE'};relative='资源\二.mkv';pending=$false}
    $script:leaves=@($script:leaf1,$script:leaf2);$script:rootPhase='PHASE_TYPE_COMPLETE';$script:created=0;$script:cacheCalls=0;$script:deleteCalls=0;$script:snapshotCalls=0
    function Invoke-PanApi($Connection,$Method,$Arguments) {
        if($Method -eq 'getFileInfo'){return @{id=$Arguments[0];kind='drive#folder';phase=$script:rootPhase;user_id='owner';parent_id='root';trashed=$false}}
        if($Method -eq 'batchCreateDownloadTaskWithFiles'){$script:nativeOutput=$Arguments[1];return @{taskIds=@(777)}}
        if($Method -eq 'createFile'){$script:cacheCalls++;return @{task=@{id='cache'};file=@{id='cachefile'}}}
        throw ('禁止非模拟云盘调用：'+$Method)
    }
    function Invoke-ThunderApi($Connection,$Method,$Arguments) {
        if($Method -eq 'CategoryManagerCategoryGetDownloadingTaskIds'){return @{data=@([long]$script:candidate.taskId)}}
        if($Method -eq 'TaskManagerTaskBtTaskGetBtFileInfos'){return @{data=$script:bt}}
        if($Method -eq 'TaskManagerTaskGetTaskBase'){return @{data=$script:candidate}}
        if($Method -eq 'TaskManagerTaskDeleteTask'){$script:deleteCalls++;$script:deletedFiles=$Arguments[1];$script:candidate.recycleTime=[long]999;return @{data=0}}
        throw ('禁止非模拟本地调用：'+$Method)
    }
    function Get-CloudTree($Connection,$Parent,$Relative='', $Depth=0){return $script:leaves}
    $job=@{id='99';name='规则验证';folderId='job';folderName='任务-99';cacheFileId='cachefile';btFiles=$script:bt;downloads=@{};original=$candidate;stage='caching';manualMode=$true;downloadNotBefore=1300;error=''}
    $snapshot=Get-CompleteSnapshot $null $job
    Assert-Test ($snapshot.ready -and $snapshot.leaves.Count -eq 2) '整项全部缓存完成才可下载'
    $script:leaf2.pending=$true
    $snapshot=Get-CompleteSnapshot $null $job
    Assert-Test (-not $snapshot.ready) '一个文件未缓存完时整项跳过'
    $script:leaf2.pending=$false;$script:leaves=@($script:leaf1)
    $snapshot=Get-CompleteSnapshot $null $job
    Assert-Test (-not $snapshot.ready) '目录只列出已完成部分时按原BT清单识别缺失文件'
    $script:leaves=@($script:leaf1,$script:leaf2);$script:rootPhase='PHASE_TYPE_RUNNING'
    Assert-Test (-not (Get-CompleteSnapshot $null $job).ready) '缓存根资源未完成时不下载子文件'
    $script:rootPhase='PHASE_TYPE_COMPLETE'
    function Process-ReadyFile($Connection,$Job,$Leaf){$script:created++;$Job.downloads[[string]$Leaf.file.id]=@{fileId=$Leaf.file.id;intent=$true;stage='created'}}
    $script:State.jobs=@{'99'=$job}
    Invoke-WholeDownload $null $job @{ready=$false;reason='未完成';leaves=@()}
    Assert-Test ($script:created -eq 0 -and $job.stage -eq 'manual') '未完成资源不产生任何下载'
    $script:State.jobs['other']=@{downloads=@{one=@{fileId='one';intent=$true;stage='created'}}}
    Invoke-WholeDownload $null $job (Get-CompleteSnapshot $null $job)
    Assert-Test ($script:created -eq 1 -and $job.downloads.ContainsKey('two')) '全局文件ID排除已安排下载资源'
    $tracked=Get-TrackedFiles
    Assert-Test ($tracked.ContainsKey('one') -and $tracked.ContainsKey('two')) '排队中的下载同样持久去重'
    function Register-Once($Request,[long]$At){$script:scheduledAt=$At;$Request.notBefore=$At;Save-Request $Request}
    function Sync-RequestedDownloads($Connection,$Jobs){$script:synced=@($Jobs).Count}
    $request=@{id='11111111111111111111111111111111';stage='waiting';notBefore=1300;jobIds=@('99');cacheOnly=$false}
    Save-Request $request
    $opts=@{command='Due';request=$request.id}
    $null=Invoke-ManualCommand $null $opts
    Assert-Test ($script:scheduledAt -eq 1300 -and (Read-Request $request.id).stage -eq 'waiting') '五分钟前只安排到点执行，不扫描或下载'
    # 将原函数保留为真实快照测试结果；后续用调用计数验证只扫描一次。
    function Get-CompleteSnapshot($Connection,$Job){$script:snapshotCalls++;return @{ready=$false;reason='仍有一个未完成';leaves=@()}}
    $job.stage='caching';$script:clock=1300
    $null=Invoke-ManualCommand $null $opts
    $request=Read-Request $request.id
    Assert-Test ($script:snapshotCalls -eq 1 -and $request.stage -eq 'checked' -and -not $request.results[0].ready) '五分钟到点仅检查一次，未完成留待下次'
    $script:clock=1600;$null=Invoke-ManualCommand $null $opts
    Assert-Test ($script:snapshotCalls -eq 1) '再次触发同一请求不会下载后来缓存出的文件'
    $request.stage='checking';Save-Request $request;$null=Invoke-ManualCommand $null $opts
    Assert-Test ($script:snapshotCalls -eq 1) '中断恢复不会重扫已消费的一次检查'
    $script:State.jobs=@{};$candidate.recycleTime=[long]123;$candidate.taskStatus=7
    $recycleOptions=@{limit=20;wait=5;output='D:\新位置';cacheOnly=$false;request='22222222222222222222222222222222'}
    $rjob=New-ManualJob $null $candidate 'recycle' $recycleOptions
    function Get-OwnedFolder($Connection){return 'root'}
    $rjob.folderId='job'
    Start-Job $null $rjob
    Assert-Test ($script:deleteCalls -eq 0 -and $script:cacheCalls -eq 1 -and $rjob.source -eq 'recycle') '回收站提交阶段只缓存，不提前清理原记录'
    Assert-Test ($rjob.downloadNotBefore -eq 1900 -and $rjob.waitSeconds -eq 300) '每项持久保存五分钟等待时间及目标路径'
    Assert-Test ($rjob.outputRoot -eq 'D:\新位置') '指定保存路径进入持久任务记录'
    $caught=$false;try{$null=New-ManualJob $null $candidate 'recycle' $recycleOptions}catch{$caught=$true}
    Assert-Test $caught '回收站重复提交被持久记录拦截'
    $candidate.taskId=[long]100;$candidate.taskStatus=5;$candidate.recycleTime=[long]0
    $activeJob=New-ManualJob $null $candidate 'active' $recycleOptions
    $activeJob.folderId='job';$candidate.downloadSpeed=[long]1;$caught=$false
    try{Start-Job $null $activeJob}catch{$caught=$true}
    Assert-Test ($caught -and $script:deleteCalls -eq 0) '删除前再次核对zero20，速度恢复就取消删除'
    $candidate.downloadSpeed=[long]0
    Start-Job $null $activeJob
    Assert-Test ($script:deleteCalls -eq 1 -and $script:deletedFiles -eq $false -and $activeJob.stage -eq 'caching') '符合zero20才删除任务并转存，删除调用明确保留本地文件'
    function Find-RecoveryTask($Connection,$Record){return 0}
    $nativeJob=@{id='new';name='资源';folderName='任务-new';folderId='job';manualMode=$true;btFiles=@();downloads=@{};original=@{savePath='D:\原路径'};outputRoot=$testDir}
    & $nativeProcess $null $nativeJob $script:leaf1
    $expected=Join-Path (Join-Path $testDir '任务-new') '资源'
    Assert-Test ($script:nativeOutput -eq $expected -and $nativeJob.downloads.one.taskId -eq 777) '原生下载接口使用指定本地目录并保留资源相对层级'
    Assert-Test ($nativeJob.downloads.one.archiveMode -eq 'downloaded') '新增下载明确使用已下载归档策略'
    function Get-CheckedFolder($Connection,$Parent,$Name){return ($Parent+'/'+$Name)}
    function Assert-OwnedDescendant($Connection,$Folder,$Ancestor){}
    $nativeJob.manualMode=$false
    $target=Get-MoveTarget $null $nativeJob $nativeJob.downloads.one
    Assert-Test ($target -eq 'root/已下载/任务-new/资源') '旧转存任务的新下载也进入已下载目录并避免旧移动路径冲突'
    # 用内存云盘执行完整转存入口，验证同批共享、跨批分离和扫描粒度。
    Set-Item Function:Get-CheckedFolder $nativeCheckedFolder
    $script:State.jobs=@{};$script:State.batches=@{}
    $script:folders=@{};$script:testTasks=@{};$script:folderCreates=0
    $previousPan=${function:Invoke-PanApi};$previousThunder=${function:Invoke-ThunderApi}
    function Get-Children($Connection,[string]$ParentId){return @($script:folders.Values|Where-Object {$_.parent_id -eq $ParentId})}
    function Invoke-PanApi($Connection,$Method,$Arguments) {
        if($Method -eq 'createFolder') {
            $id=$Arguments[0]+'/'+$Arguments[1];$script:folderCreates++
            $f=@{id=$id;name=$Arguments[1];parent_id=$Arguments[0];user_id='owner';kind='drive#folder';trashed=$false}
            $script:folders[$id]=$f;return @{file=$f}
        }
        if($Method -eq 'getFileInfo' -and $script:folders.ContainsKey([string]$Arguments[0])){return $script:folders[[string]$Arguments[0]]}
        return & $previousPan $Connection $Method $Arguments
    }
    function Invoke-ThunderApi($Connection,$Method,$Arguments) {
        if($Method -eq 'TaskManagerTaskGetTaskBase' -and $script:testTasks.ContainsKey([string]$Arguments[0])){return @{data=$script:testTasks[[string]$Arguments[0]]}}
        return & $previousThunder $Connection $Method $Arguments
    }
    function Get-RecycleTasks($Connection){return @($script:testTasks.Values)}
    foreach($id in @(200,201)){$t=$candidate.Clone();$t.taskId=[long]$id;$t.recycleTime=[long]999;$t.taskType=1;$script:testTasks[[string]$id]=$t}
    $options=@{command='Recycle';output='D:\批次测试';ids=@();all=$true;dry=$false;cacheOnly=$true;wait=5;limit=20}
    $first=Invoke-ManualCommand $null $options
    $firstName=$first.request.batchFolderName
    Assert-Test ($firstName -match '^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-[a-f0-9]{8}$') '批次目录使用日期时间及请求编号，名称兼容Windows'
    $firstJobs=@($script:State.jobs.Values)
    Assert-Test ($firstJobs.Count -eq 2 -and $firstJobs[0].batchFolderId -eq $firstJobs[1].batchFolderId -and $script:folderCreates -eq 3) '一次命令的两个任务共用一个批次目录，各自创建任务目录'
    Assert-Test (@($firstJobs|Where-Object {$script:folders[$_.folderId].parent_id -ne $_.batchFolderId}).Count -eq 0) '任务目录实际创建在批次下面'
    Assert-Test ((Read-Request $first.request.id).batchFolderName -eq $firstName) '批次名称持久保存到请求记录'
    $firstJobs[0].folderId='';$before=$script:folderCreates;Start-Job $null $firstJobs[0]
    Assert-Test ($script:folderCreates -eq $before -and $firstJobs[0].batchFolderName -eq $firstName) '恢复任务复用原批次和任务目录，不重复创建'
    $t=$candidate.Clone();$t.taskId=[long]202;$t.recycleTime=[long]999;$t.taskType=1;$script:testTasks=@{'202'=$t}
    $second=Invoke-ManualCommand $null $options
    Assert-Test ($second.request.batchFolderName -ne $firstName -and $script:State.batches.Count -eq 2) '同一秒再次执行命令也创建独立批次'
    $before=$script:folderCreates;$options.dry=$true;$null=Invoke-ManualCommand $null $options
    Assert-Test ($script:folderCreates -eq $before -and $script:State.batches.Count -eq 2) '预览不创建批次目录'
    $options.dry=$false;$script:testTasks=@{};$null=Invoke-ManualCommand $null $options
    Assert-Test ($script:folderCreates -eq $before -and $script:State.batches.Count -eq 2) '没有匹配任务时不创建空批次目录'
    $script:folders['root/旧任务']=@{id='root/旧任务';name='任务-旧';parent_id='root';user_id='owner';kind='drive#folder'}
    $script:State.jobs['legacy']=@{id='legacy';folderId='root/旧任务';folderName='任务-旧';downloads=@{}}
    $script:folders['root/archive']=@{id='root/archive';name='已下载';parent_id='root';user_id='owner';kind='drive#folder'}
    $script:folders['root']=@{id='root';name='专属总目录';parent_id='';user_id='owner';kind='drive#folder'}
    $units=@(Get-CloudUnits $null '')
    Assert-Test ($units.Count -eq 4 -and @($units|Where-Object {$_.id -eq 'legacy'}).Count -eq 1) '根目录扫描穿透时间批次并兼容旧任务，归档不计入待下载'
    Assert-Test (@($units|Where-Object {$_.id -in @('200','201','202')}).Count -eq 3) '批次里的每个任务独立判断完整性，不能把整批合成一个资源'
    $scoped=@(Get-CloudUnits $null $firstJobs[0].batchFolderId)
    Assert-Test ($scoped.Count -eq 2) '指定批次目录时只扫描该批任务'
    $firstJobs[0].downloads=@{};$firstJobs[0].moveFolders=@{}
    $record=@{relative='资源\一.mkv';destinationId=''}
    $archiveTarget=Get-MoveTarget $null $firstJobs[0] $record
    Assert-Test ($archiveTarget -eq ('root/已下载/'+$firstName+'/'+$firstJobs[0].folderName+'/资源')) '已启动下载按批次及任务层级归档'
    @{passed=$script:passed.Count;tests=$script:passed;liveCloudCalls=0}|ConvertTo-Json -Depth 6
}finally{Remove-Item -LiteralPath $testDir -Recurse -Force -ErrorAction SilentlyContinue}
