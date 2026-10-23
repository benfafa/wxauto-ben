# Self-healing watchdog for the wx-bridge stack. Runs as SYSTEM every minute.
#
# Two failure modes this covers, both observed on the Tencent Lighthouse box:
#   1. The RDP client closes -> the Administrator session is left with no desktop
#      to render, and every WeChat UI-Automation call starts failing.
#   2. The stack supervisor itself dies -> nothing relaunches the collector, so
#      the bridge spins on "fetch failed" forever.
#
# Register with (or just run the equivalent from a TAT command slot):
#   $a = New-ScheduledTaskAction -Execute 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' `
#        -Argument '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\wxb\deploy\watchdog.ps1'
#   $p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
#   $t = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1) `
#        -RepetitionDuration (New-TimeSpan -Days 9999)
#   Register-ScheduledTask -TaskName 'wxb-watchdog' -Action $a -Principal $p -Trigger $t `
#        -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew) -Force
#
# ASCII only, and no localized status strings: `query session` on this box prints
# 活动/已断开, so a disconnected session is detected by its SESSIONNAME column
# being empty (the first token stops being rdp-tcp#N / console).
$ErrorActionPreference = 'SilentlyContinue'
$log = 'C:\wxb\logs\watchdog.log'

function Say($message) {
    Add-Content -Path $log -Value ('[' + (Get-Date -Format 'HH:mm:ss') + '] ' + $message)
}

$rows = @(query session | Where-Object { $_ -match 'Administrator' })
if ($rows.Count -gt 0) {
    $tokens = @($rows[0] -split ' +' | Where-Object { $_ })
    $id = @($tokens | Where-Object { $_ -match '^[0-9]+$' })[0]
    if ($tokens[0] -ne 'rdp-tcp#0' -and $tokens[0] -ne 'console' -and $id) {
        $out = (tscon $id /dest:console 2>&1 | Out-String)
        Say ('reattach session ' + $id + ' -> ' + (($out -split ' +') -join ' ').Trim())
    }
}

$supervisor = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'powershell.exe' -and $_.CommandLine -like '*start-stack*'
})
if ($supervisor.Count -eq 0) {
    Start-ScheduledTask -TaskName 'wxb-start'
    Say 'supervisor missing -> started wxb-start'
}
