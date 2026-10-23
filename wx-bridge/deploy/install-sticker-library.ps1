# Installs the labelled sticker library and the updated wx-bridge code on the server.
#
# Upload this file plus the release zip to C:\wxb\deploy\, then run it detached,
# because a TAT command slot only gives you 60 seconds:
#   Start-Process powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','C:\wxb\deploy\install-sticker-library.ps1' -WindowStyle Hidden
#
# Expected zip layout (code tree plus scripts/build-sticker-library.py output):
#   wx-bridge/              src/, collector/, preset/, scripts/, package.json
#   release/stickers/       curated images
#   release/sticker-index.json
# config.json, node_modules/ and state/ are never overwritten by the code copy.
#
# ASCII only on purpose: Windows PowerShell 5.1 plus the console codepage mangles
# non-ASCII script files, and this has to survive a TAT session.
param(
    [string]$Root = 'C:\wxb',
    [string]$Zip = 'C:\wxb\deploy\sticker-release.zip',
    # When the images already live on the server (uploaded once, e.g. a scratch
    # lab folder), the script copies only the files named by the index from here
    # and verifies each sha256. Saves re-uploading 300MB through the console.
    [string]$LabDirectory = '',
    # Deleting is never a side effect of an install: without -Prune the extra
    # files are only listed in logs\sticker-prune.txt for review.
    [switch]$Prune
)

$ErrorActionPreference = 'Stop'
$log = Join-Path $Root 'logs\deploy-stickers.log'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

function Say($message) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $message
    Write-Output $line
    Add-Content -Path $log -Value $line -Encoding UTF8
}

$staging = Join-Path $env:TEMP ("wxb-stickers-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $staging | Out-Null
try {
    if (-not (Test-Path $Zip)) { throw "zip not found: $Zip" }
    Say "expanding $Zip"
    Expand-Archive -LiteralPath $Zip -DestinationPath $staging -Force

    $bridge = @(Get-ChildItem -Path $staging -Recurse -Filter 'bridge.js' | Select-Object -First 1)
    if (-not $bridge) { throw "src/bridge.js missing from the zip" }
    $codeRoot = $bridge[0].Directory.FullName
    Say "code root: $codeRoot"

    $indexFile = @(Get-ChildItem -Path $staging -Recurse -Filter 'sticker-index.json' | Select-Object -First 1)
    if (-not $indexFile) { throw "sticker-index.json missing from the zip" }
    $indexSource = $indexFile[0].FullName
    $images = Join-Path (Split-Path $indexSource) 'stickers'
    if (-not (Test-Path $images) -and -not $LabDirectory) { throw "no stickers/ next to the index and no -LabDirectory" }
    if ($LabDirectory -and -not (Test-Path $LabDirectory)) { throw "lab directory not found: $LabDirectory" }

    # Code first, config last, so a crash never leaves a config pointing at
    # files that are not on disk yet.
    foreach ($item in @('src', 'collector', 'preset', 'scripts')) {
        $from = Join-Path $codeRoot $item
        if (Test-Path $from) {
            Copy-Item -Path $from -Destination $Root -Recurse -Force
            Say "updated $item"
        }
    }
    Copy-Item -Path (Join-Path $codeRoot 'package.json') -Destination $Root -Force -ErrorAction SilentlyContinue

    $targetStickers = Join-Path $Root 'stickers'
    New-Item -ItemType Directory -Force -Path $targetStickers | Out-Null
    Copy-Item -Path $indexSource -Destination (Join-Path $Root 'sticker-index.json') -Force
    if ($LabDirectory) {
        # Images are already on the server; rebuild the live folder from the index
        # and verify every sha256 instead of uploading 300MB again.
        $copyPy = Join-Path $Root 'scripts\copy-library-by-index.py'
        & C:\Python311\python.exe $copyPy --index (Join-Path $Root 'sticker-index.json') `
            --src $LabDirectory --dst $targetStickers 2>&1 | ForEach-Object { Say "copy: $_" }
    }
    else {
        Copy-Item -Path (Join-Path $images '*') -Destination $targetStickers -Force
    }
    $count = (Get-ChildItem $targetStickers -File | Measure-Object).Count
    Say "stickers: $count files in $targetStickers"

    # Anything in the live folder that the curated release does not know about is
    # reported, not removed, unless -Prune was passed explicitly.
    $release = @((Get-Content -Raw -Path (Join-Path $Root 'sticker-index.json') | ConvertFrom-Json).PSObject.Properties.Name)
    $extra = @(Get-ChildItem $targetStickers -File | Where-Object { $release -notcontains $_.Name })
    $pruneLog = Join-Path $Root 'logs\sticker-prune.txt'
    if ($extra.Count) {
        $extra | Select-Object -ExpandProperty Name | Set-Content -Path $pruneLog -Encoding UTF8
        Say "$($extra.Count) extra file(s) not in the index; list: $pruneLog"
        if ($Prune) {
            $extra | Remove-Item -Force
            Say "removed those $($extra.Count) file(s) because -Prune was set"
        }
    } else {
        Set-Content -Path $pruneLog -Value '' -Encoding UTF8
        Say 'live folder matches the index exactly'
    }

    $configPath = Join-Path $Root 'config.json'
    $config = Get-Content -Raw -Path $configPath | ConvertFrom-Json
    $stickers = [PSCustomObject]@{
        enabled        = $true
        directory      = 'stickers'
        autoLoad       = $true
        cooldownMs     = 0
        items          = @()
        indexFile      = 'sticker-index.json'
        shortlistLimit = 20
    }
    if ($config.PSObject.Properties.Name -contains 'stickers') {
        $config.stickers = $stickers
    }
    else {
        $config | Add-Member -NotePropertyName stickers -NotePropertyValue $stickers
    }
    if (-not ($config.PSObject.Properties.Name -contains 'vision')) {
        $vision = [PSCustomObject]@{ enabled = $true; contextWindowMs = 120000; maxImages = 2; maxBytes = 5242880 }
        $config | Add-Member -NotePropertyName vision -NotePropertyValue $vision
    }
    elseif (-not $config.vision.enabled) {
        Say 'vision exists but enabled=false; left as is'
    }
    [IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 12), (New-Object Text.UTF8Encoding($false)))
    Say 'config.json updated with stickers/vision'

    $checkPy = Join-Path $Root 'scripts\check-sticker-catalog.py'
    & C:\Python311\python.exe $checkPy $configPath 2>&1 | ForEach-Object { Say "collector: $_" }
    & node (Join-Path $Root 'scripts\check-sticker-config.mjs') $configPath 2>&1 | ForEach-Object { Say "node: $_" }

    # Only the bridge and the collector are stopped; DSH and WeChat stay up and the
    # supervisor relaunches what died within about 30 seconds.
    Say 'restarting bridge and collector'
    Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
        Where-Object { $_.CommandLine -like '*bridge.js*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*wx_collector*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Say 'done'
}
catch {
    Say "FAILED: $($_.Exception.Message)"
    exit 1
}
finally {
    Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
}
