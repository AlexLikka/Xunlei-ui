[CmdletBinding()]
param([string]$ModuleDir=$PSScriptRoot)
$ErrorActionPreference='Stop'
$testDir=Join-Path $env:TEMP ('Thunder-RecycleCleanup-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testDir -Force|Out-Null
try {
 . (Join-Path $ModuleDir 'manual.ps1') -WorkDir $testDir -LibraryOnly
 $script:passed=@();$script:clock=1000
 function Assert-Test([bool]$Value,[string]$Name){if(-not $Value){throw ('FAIL: '+$Name)};$script:passed+=,$Name}
 function Get-Now{return $script:clock}
 function Write-Event($Event,$Details){}
 function Save-State{$script:savedIntent=$script:job.recycleCleanupIntent}
 function Register-RecycleCleanup($Request,[long]$At){$script:registered++;$script:scheduledAt=$At}
 function Unregister-ScheduledTask{param($TaskName,[switch]$Confirm,$ErrorAction);$script:unregistered++}
 $script:original=@{taskId=[long]99;taskName='fixture';savePath='C:\fixture';url='https://example.com/fixture';fileSize=[long]10;taskType=1;recycleTime=[long]999;isPanTask=$false}
 function New-Fixture {
  $script:job=@{id='99';name='fixture';original=$script:original.Clone();source='recycle';cleanupRecycle=$true;cacheAcknowledged=$true;recycleCleanup='waiting';folderId='cloud-folder';downloads=@{}}
  $script:fresh=$script:original.Clone();$script:present=$true;$script:deleteCalls=0;$script:nullList=$false;$script:reject=$false;$script:timeout=$false;$script:keepRecord=$false;$script:registered=0;$script:unregistered=0
  $script:State=@{schema=1;jobs=@{'99'=$script:job};ownerId='owner'}
 }
 function Invoke-ThunderApi($Connection,$Method,$Arguments) {
  if($Method -eq 'CategoryManagerCategoryViewGetTasks') {
   Assert-Test ($Arguments.Count -eq 2 -and $Arguments[0] -eq -1 -and $Arguments[1] -eq 3) 'only recycle view queried'
   if($script:nullList){return @{data=$null}}
   if($script:present){return @{data=@([long]99)}}
   return @{data=@()}
  }
  if($Method -eq 'TaskManagerTaskGetTaskBase'){Assert-Test ($Arguments[0] -eq 99) 'only frozen ID queried';return @{data=$script:fresh}}
  if($Method -eq 'TaskManagerTaskDeleteTask') {
   $script:deleteCalls++
   Assert-Test ($Arguments.Count -eq 2 -and $Arguments[0] -eq 99 -and $Arguments[1] -eq $false) 'single ID deletion explicitly preserves files'
   Assert-Test ($script:savedIntent -eq $true) 'durable intent before mutation'
   if($script:timeout){throw '迅雷接口响应超时'}
   if($script:reject){return @{data=-1}}
   if(-not $script:keepRecord){$script:present=$false}
   return @{data=0}
  }
  throw ('unexpected native method: '+$Method)
 }
 $ready=@{ready=$true;leaves=@()};$notReady=@{ready=$false;reason='pending';leaves=@()}
 New-Fixture;Clear-TransferredRecycle $null $script:job $notReady
 Assert-Test ($script:deleteCalls -eq 0) 'pending cache retained'
 $script:job.cacheAcknowledged=$false;Clear-TransferredRecycle $null $script:job $ready
 Assert-Test ($script:deleteCalls -eq 0) 'unknown cache submission retained'
 New-Fixture;$script:job.cleanupRecycle=$false;Clear-TransferredRecycle $null $script:job $ready
 Assert-Test ($script:deleteCalls -eq 0) 'legacy opt-out never retroactively removed'
 New-Fixture;$script:job.source='active';Clear-TransferredRecycle $null $script:job $ready
 Assert-Test ($script:deleteCalls -eq 0) 'ordinary transfers never clear recycle records'
 foreach($field in @('taskName','url','savePath','fileSize','taskType','recycleTime','isPanTask')) {
  New-Fixture
  if($field -eq 'isPanTask'){$script:fresh[$field]=$true}
  elseif($field -in @('fileSize','taskType','recycleTime')){$script:fresh[$field]=123456}
  else{$script:fresh[$field]='changed'}
  Clear-TransferredRecycle $null $script:job $ready
  Assert-Test ($script:deleteCalls -eq 0 -and $script:job.recycleCleanup -eq 'changed') ('identity change retained: '+$field)
 }
 New-Fixture;$script:fresh.recycleTime=0;Clear-TransferredRecycle $null $script:job $ready
 Assert-Test ($script:deleteCalls -eq 0 -and $script:job.recycleCleanup -eq 'changed') 'restored task retained'
 New-Fixture;$script:present=$false;Clear-TransferredRecycle $null $script:job $ready
 Assert-Test ($script:deleteCalls -eq 0 -and $script:job.recycleCleanup -eq 'absent') 'externally absent task not deleted'
 New-Fixture;$script:nullList=$true;$caught=$false;try{Clear-TransferredRecycle $null $script:job $ready}catch{$caught=$true}
 Assert-Test ($caught -and $script:deleteCalls -eq 0) 'missing recycle view fails closed'
 New-Fixture;Clear-TransferredRecycle $null $script:job $ready
 Assert-Test ($script:deleteCalls -eq 1 -and -not $script:present -and $script:job.recycleCleanup -eq 'cleaned') 'complete resource removes only matching recycle entry'
 Clear-TransferredRecycle $null $script:job $ready
 Assert-Test ($script:deleteCalls -eq 1) 'successful cleanup never repeated'
 foreach($failure in @('reject','timeout','keepRecord')) {
  New-Fixture;Set-Variable -Scope Script -Name $failure -Value $true
  $caught=$false;try{Clear-TransferredRecycle $null $script:job $ready}catch{$caught=$true}
  Assert-Test ($caught -and $script:job.recycleCleanup -eq 'uncertain') ('unconfirmed result protected: '+$failure)
  Clear-TransferredRecycle $null $script:job $ready
  Assert-Test ($script:deleteCalls -eq 1) ('no destructive retry: '+$failure)
 }
 New-Fixture
 function Get-CompleteSnapshot($Connection,$Job){return @{ready=$script:cacheReady;leaves=@()}}
 $script:cacheReady=$false
 $request=@{id='11111111111111111111111111111111';jobIds=@('99');stage='cacheOnly';cacheOnly=$true;cleanupRecycle=$true;cleanupDeadline=2000;cleanupScheduledTask='test-task'}
 $null=Invoke-RecycleCleanup $null $request
 Assert-Test ($script:registered -eq 1 -and $script:scheduledAt -eq 1300 -and $request.cleanupPending -eq 1 -and $script:deleteCalls -eq 0) 'cache-only request polls incomplete cache without download'
 $script:cacheReady=$true;$null=Invoke-RecycleCleanup $null $request
 Assert-Test ($request.cleanupCleaned -eq 1 -and $request.cleanupStage -eq 'done' -and $script:unregistered -eq 1) 'successful cleanup unregisters its own scheduled task'
 New-Fixture;$script:cacheReady=$true
 $other=$script:job.Clone();$other.id='100';$other.cacheAcknowledged=$false
 $script:State.jobs['100']=$other;$request.jobIds=@('99','100');$request.cleanupDeadline=2000
 $null=Invoke-RecycleCleanup $null $request
 Assert-Test ($script:deleteCalls -eq 1 -and $script:job.recycleCleanup -eq 'cleaned' -and $other.recycleCleanup -eq 'waiting' -and $request.cleanupPending -eq 1) 'one failed or incomplete item does not block successful item'
 $request.jobIds=@('99')
 New-Fixture;$script:clock=2001;$request.cleanupDeadline=2000;$null=Invoke-RecycleCleanup $null $request
 Assert-Test ($script:job.recycleCleanup -eq 'expired' -and $script:deleteCalls -eq 0 -and $script:registered -eq 0) 'expiry retains original entry without perpetual polling'
 New-Fixture;$script:clock=1000;$request.cleanupRecycle=$false;$null=Invoke-RecycleCleanup $null $request
 Assert-Test ($script:deleteCalls -eq 0 -and $script:registered -eq 0) 'historical request remains read-only'
 @{passed=$script:passed.Count;tests=$script:passed;liveCloudCalls=0;liveDeleteCalls=0}|ConvertTo-Json -Depth 6
}finally{Remove-Item -LiteralPath $testDir -Recurse -Force -ErrorAction SilentlyContinue}
