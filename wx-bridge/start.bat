@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist config.json (
  echo [wx-bridge] config.json is missing.
  echo Run: copy config.example.json config.json
  echo Then fill wechat.groups / wechat.mentionAliases / wechat.token / dsh.*
  pause
  exit /b 1
)

if not exist node_modules (
  echo [wx-bridge] node_modules is missing. Run: npm install
  pause
  exit /b 1
)

echo [wx-bridge] Starting collector (WeChat must be logged in and the desktop unlocked)...
start "wx-collector" cmd /k "python -m collector.wx_collector --config config.json"

echo [wx-bridge] Starting bridge (waits up to 60s for the collector)...
start "wx-bridge" cmd /k "node src\bridge.js config.json"

echo.
echo Two windows started. Close them or press Ctrl+C to stop each process.
pause
