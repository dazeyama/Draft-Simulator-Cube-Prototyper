@echo off
REM Start the CSV Trait Editor, open it in the browser, then wait for SPACE to stop.
cd /d "%~dp0"
echo Starting CSV Trait Editor on http://localhost:8003 ...
start "csvtool" python "%~dp0app.py"
timeout /t 2 /nobreak >nul
start "" "http://localhost:8003"
echo.
echo CSV Trait Editor is running at http://localhost:8003
echo.
echo Press SPACE to end program.

:wait
REM SPACE -> exit 0 (stop) ; BACKSPACE -> exit 7 (secret: restart server for updates)
powershell -NoProfile -Command "while($true){ $k=[Console]::ReadKey($true).Key; if($k -eq 'Spacebar'){exit 0}; if($k -eq 'Backspace'){exit 7} }"
if "%errorlevel%"=="7" goto restart
goto stop

:restart
echo.
echo Restarting server...
call "%~dp0stop.bat" >nul 2>&1
start "csvtool" python "%~dp0app.py"
timeout /t 1 /nobreak >nul
echo Server restarted - refresh your browser (Ctrl+F5).
echo.
goto wait

:stop
echo.
echo Stopping...
call "%~dp0stop.bat"
