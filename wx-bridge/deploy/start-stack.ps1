# wx-bridge 服务器端守护脚本（必须在已登录的桌面会话里运行）
# 负责：微信 / DSH / 采集端 / 桥接 四个进程的启动与保活，并在首次运行时安装 wx-chat preset。
# 每个组件都用 run-<name>.bat 包装（cmd 直接 /c 调用 .cmd + 重定向会被引号规则吃掉）。

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

$root = 'C:\wxb'
$logs = Join-Path $root 'logs'
$deploy = Join-Path $root 'deploy'
$weixinExe = 'C:\Program Files\Tencent\Weixin\Weixin.exe'
$patchFile = Join-Path $env:USERPROFILE '.dsh\profiles\web\cordis.patch.yml'
$marker = 'wx-bridge preset BEGIN'

New-Item -ItemType Directory -Force $logs, $deploy | Out-Null

# 单实例：已有别的 start-stack.ps1 在跑就直接退出
$selfPid = $PID
$others = @(Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $selfPid -and $_.CommandLine -and $_.CommandLine -match 'start-stack\.ps1'
    })
if ($others.Count -gt 0) {
    "duplicate supervisor detected (pid=$($others[0].ProcessId)), exit" |
        Add-Content -Path (Join-Path $logs 'stack.log') -Encoding UTF8
    exit 0
}

$machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$env:Path = "$machinePath;$userPath;C:\npm-global"

function Write-Log($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Add-Content -Path (Join-Path $logs 'stack.log') -Encoding UTF8
}

function Test-Port($port) {
    try {
        $client = New-Object Net.Sockets.TcpClient
        $client.Connect('127.0.0.1', $port)
        $client.Close()
        return $true
    } catch {
        return $false
    }
}

function Get-MatchProcess([string]$pattern) {
    Get-CimInstance Win32_Process -Filter "Name='node.exe' or Name='python.exe' or Name='cmd.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $pattern }
}

function Write-Runner([string]$name, [string]$body) {
    $lines = @('echo off', "cd /d $root", "call $body >> `"$logs\$name.log`" 2>&1")
    Set-Content -Path (Join-Path $deploy "run-$name.bat") -Value $lines -Encoding ASCII
}

function Start-Runner([string]$name) {
    Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c', (Join-Path $deploy "run-$name.bat")) `
        -WorkingDirectory $root -WindowStyle Hidden
    Write-Log "started $name"
}

function Stop-Match([string]$pattern, [string]$why) {
    foreach ($proc in @(Get-MatchProcess $pattern)) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Log "stopped pid=$($proc.ProcessId) ($why)"
    }
}

# Identity of the WeChat main process, used to notice a real restart (update,
# crash, manual relaunch) so the collector can drop its now-dead UIA handles.
#
# Deliberately PID + start time only. MainWindowHandle flips whenever another
# window becomes the process's "main" one — opening a detached chat window did
# that every ~40 seconds and restarted the collector each time. A restart that
# keeps the process and only rebuilds the window is handled inside the
# collector instead: it demotes to main-window polling when a detached window
# disappears, and re-adopts it once it is back.
function Get-WeixinKey {
    $procs = @(Get-Process Weixin -ErrorAction SilentlyContinue | Sort-Object StartTime)
    if ($procs.Count -eq 0) { return $null }
    return ('' + $procs[0].Id + '|' + $procs[0].StartTime.ToFileTime())
}

Write-Runner 'dsh' 'C:\npm-global\dsh.cmd web --no-open'
Write-Runner 'dsh-init' 'C:\npm-global\dsh.cmd web --no-open'
Write-Runner 'collector' 'python -m collector.wx_collector --config config.json'
Write-Runner 'bridge' 'node src\bridge.js config.json'

function Ensure-PresetInstalled {
    if (-not (Test-Path $patchFile)) {
        Write-Log 'preset: profile 还不存在，先启动一次 dsh web 生成'
        Start-Runner 'dsh-init'
        $deadline = (Get-Date).AddSeconds(90)
        while (-not (Test-Path $patchFile) -and (Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 3
        }
        Stop-Match 'dsh-init|@deepseek-ai.dsh' 'preset-init-done'
        Start-Sleep -Seconds 3
    }
    if (-not (Test-Path $patchFile)) {
        Write-Log 'preset: 仍然找不到 cordis.patch.yml，稍后重试'
        return
    }
    if ((Get-Content $patchFile -Raw) -match [regex]::Escape($marker)) {
        return
    }
    Push-Location $root
    $result = node scripts\install-preset.mjs --profile web 2>&1 | Out-String
    Pop-Location
    Write-Log ("preset: " + $result.Trim())
    Stop-Match '@deepseek-ai.dsh' 'preset-changed'
}

Write-Log '=== stack supervisor start ==='
Ensure-PresetInstalled

$lastWeixinKey = $null

while ($true) {
    try {
        if (-not (Get-Process Weixin -ErrorAction SilentlyContinue)) {
            if (Test-Path $weixinExe) {
                Start-Process $weixinExe
                Write-Log 'started Weixin'
                Start-Sleep -Seconds 20
            } else {
                Write-Log 'WARN: 微信未安装，找不到 Weixin.exe'
            }
        }

        $weixinKey = Get-WeixinKey
        if ($weixinKey -and $lastWeixinKey -and ($weixinKey -ne $lastWeixinKey)) {
            Write-Log "weixin process changed ($lastWeixinKey -> $weixinKey), restarting collector"
            Stop-Match 'collector\.wx_collector' 'weixin-restarted'
            Start-Sleep -Seconds 8
        }
        if ($weixinKey) { $lastWeixinKey = $weixinKey }

        if (-not (Test-Port 3080)) {
            Start-Runner 'dsh'
            Start-Sleep -Seconds 15
        }

        if (@(Get-MatchProcess 'collector\.wx_collector').Count -eq 0) {
            Start-Runner 'collector'
            Start-Sleep -Seconds 10
        }

        if (@(Get-MatchProcess 'src\\bridge\.js|bridge\.js config').Count -eq 0) {
            Start-Runner 'bridge'
            Start-Sleep -Seconds 5
        }
    } catch {
        Write-Log ("ERROR supervisor: " + $_.Exception.Message)
    }

    Start-Sleep -Seconds 30
}
