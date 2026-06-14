@echo off
REM Stop the CSV Trait Editor and free port 8003.
set "FOUND="
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8003 " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
    set "FOUND=1"
)
if defined FOUND (
    echo Stopped csvtool and freed port 8003.
) else (
    echo Nothing was listening on port 8003.
)
