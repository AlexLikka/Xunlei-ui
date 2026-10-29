
$ProgressPreference='SilentlyContinue'
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.Encoding]::UTF8

function Open-ThunderConnection {
    $names = @([IO.Directory]::GetFiles('\\.\pipe\') | Where-Object { $_ -match 'Thunder-xunlei-node-net-ipc-' })
    if ($names.Count -ne 1) { throw "应找到一个迅雷通信管道，实际为 $($names.Count) 个；请确认迅雷已启动且版本兼容" }
    $name = $names[0]
    if ($name.StartsWith('\\.\pipe\')) { $name = $name.Substring(9) }
    $pipe = [IO.Pipes.NamedPipeClientStream]::new('.', $name, [IO.Pipes.PipeDirection]::InOut, [IO.Pipes.PipeOptions]::Asynchronous)
    try {
        $pipe.Connect(5000)
        $conn = [pscustomobject]@{
            Pipe = $pipe
            Reader = [IO.StreamReader]::new($pipe, [Text.Encoding]::UTF8)
            Writer = [IO.StreamWriter]::new($pipe, [Text.UTF8Encoding]::new($false))
            Rid = 0
            Context = @{ name = "cli-$PID-$([Guid]::NewGuid().ToString('N'))"; productId = 'Thunder' }
        }
        $conn.Writer.AutoFlush = $true
        $null = Invoke-ThunderMessage $conn @{ action = 'register'; alias = ('thunder-' + $conn.Context.name) }
        return $conn
    } catch { $pipe.Dispose(); throw }
}

function Close-ThunderConnection($Connection) {
    if ($null -ne $Connection) {
        $Connection.Writer.Dispose()
        $Connection.Reader.Dispose()
        $Connection.Pipe.Dispose()
    }
}

function Invoke-ThunderMessage($Connection, [hashtable]$Message) {
    $rid = $Connection.Rid++
    $Message.rid = $rid
    $Message.__context = $Connection.Context
    $Message.timestamp = @([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
    $Connection.Writer.WriteLine(($Message | ConvertTo-Json -Compress -Depth 30))
    $deadline=[DateTime]::UtcNow.AddSeconds(10)
    while($true){
        $remaining=[int][Math]::Max(1,($deadline-[DateTime]::UtcNow).TotalMilliseconds)
        if([DateTime]::UtcNow -ge $deadline){throw ('迅雷接口响应超时：'+$Message.method)}
        $read=$Connection.Reader.ReadLineAsync()
        if(-not $read.Wait($remaining)){throw ('迅雷接口响应超时：'+$Message.method)}
        if($null -eq $read.Result){throw '迅雷通信连接已关闭'}
        $reply=$read.Result|ConvertFrom-Json
        if($null -eq $reply.rid -or $reply.rid -ne $rid){continue}
        if($reply.success -ne $true){throw '迅雷接口拒绝请求'}
        break
    }
    return $reply
}

function Invoke-ThunderApi($Connection, [string]$Method, [object[]]$Arguments = @()) {
    return Invoke-ThunderMessage $Connection @{ method = $Method; args = $Arguments }
}

function Get-ThunderRows($Connection,[object[]]$Ids) {
    # Pipeline a small read window on one native connection. A full list no
    # longer incurs a separate request/response wait for every task.
    for($offset=0;$offset -lt $Ids.Count;$offset+=8){
        $pending=@{};$rows=@{}
        foreach($id in @($Ids|Select-Object -Skip $offset -First 8)){
            $rid=$Connection.Rid++
            $pending[[string]$rid]=[long]$id
            $message=@{rid=$rid;__context=$Connection.Context;timestamp=@([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds());method='TaskManagerTaskGetTaskBase';args=@([long]$id)}
            $Connection.Writer.WriteLine(($message|ConvertTo-Json -Compress -Depth 10))
        }
        $deadline=[DateTime]::UtcNow.AddSeconds(15)
        while($pending.Count){
            $remaining=[int][Math]::Max(1,($deadline-[DateTime]::UtcNow).TotalMilliseconds)
            if([DateTime]::UtcNow -ge $deadline){throw '迅雷批量读取超时'}
            $read=$Connection.Reader.ReadLineAsync()
            if(-not $read.Wait($remaining)){throw '迅雷批量读取超时'}
            if($null -eq $read.Result){throw '迅雷通信连接已关闭'}
            $reply=$read.Result|ConvertFrom-Json;$key=[string]$reply.rid
            if(-not $pending.ContainsKey($key)){continue}
            if($reply.success -ne $true){throw '迅雷批量读取失败'}
            $id=$pending[$key];$pending.Remove($key)
            if($null -ne $reply.data -and $reply.data.taskId -eq $id){$rows[[string]$id]=$reply.data}
        }
        foreach($id in @($Ids|Select-Object -Skip $offset -First 8)){
            if($rows.ContainsKey([string]$id)){$rows[[string]$id]}
        }
    }
}

function Convert-ThunderTask($Task) {
    return [pscustomobject]@{
        taskId = $Task.taskId
        taskName = $Task.taskName
        taskStatus = $Task.taskStatus
        taskType = $Task.taskType
        isPanTask = $Task.isPanTask
        origin = $Task.origin
        downloadSpeed = $Task.downloadSpeed
        downloadSize = $Task.downloadSize
        fileSize = $Task.fileSize
        srcUsing = $Task.srcUsing
        srcTotal = $Task.srcTotal
        savePath = $Task.savePath
        recycleTime = $Task.recycleTime
        createTime = $Task.createTime
        completionTime = $Task.completionTime
        url = $Task.url
    }
}

function Write-CliResult($Data) {
    Write-Output ('THUNDER_CLI_JSON:' + (ConvertTo-Json -InputObject $Data -Depth 8 -Compress))
}

$connection = $null
try {
    $connection = Open-ThunderConnection
    __ACTION__
} catch {
    Write-Output ('THUNDER_CLI_ERROR:' + (ConvertTo-Json -InputObject @{ message = $_.Exception.Message } -Compress))
} finally {
    Close-ThunderConnection $connection
}
