
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
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    while ($true) {
        if([DateTime]::UtcNow -ge $deadline){throw '迅雷接口响应超时；须核对实际状态后再恢复'}
        $read = $Connection.Reader.ReadLineAsync()
        $remaining = [int][Math]::Max(1, ($deadline - [DateTime]::UtcNow).TotalMilliseconds)
        if (-not $read.Wait($remaining)) { throw '迅雷接口响应超时；须核对实际状态后再恢复' }
        if ($null -eq $read.Result) { throw '迅雷通信连接已关闭' }
        $reply = $read.Result | ConvertFrom-Json
        # 跳过无关通知，始终按照请求编号匹配响应。
        if ($null -eq $reply.rid -or $reply.rid -ne $rid) { continue }
        if ($reply.success -ne $true) { throw '迅雷接口拒绝请求，请核对客户端登录及版本' }
        break
    }
    return $reply
}

function Invoke-ThunderApi($Connection, [string]$Method, [object[]]$Arguments = @()) {
    return Invoke-ThunderMessage $Connection @{ method = $Method; args = $Arguments }
}


function Invoke-PanApi($Connection, [string]$Method, [object[]]$Arguments = @()) {
    $result = (Invoke-ThunderMessage $Connection @{
        action = 'call_remote_client_api'
        src = ('thunder-' + $Connection.Context.name).ToLower()
        dst = 'thunder-thunderpanplugin'
        method = 'Thunder_Pan_SDK_' + $Method
        args = $Arguments
    }).data
    if ($result.error -or $result.error_code) { throw '云盘接口拒绝请求，请核对登录、空间、次数及账号权限' }
    return $result
}
