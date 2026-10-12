@echo off
:: uninstall_service.bat
:: Removes the OCR Daemon from Windows Task Scheduler.
:: The daemon will no longer start automatically at login.

setlocal
set "TASK_NAME=OCR Daemon"

echo.
echo  Removing "%TASK_NAME%" from Task Scheduler...
echo.

schtasks /delete /tn "%TASK_NAME%" /f

if %errorlevel%==0 (
    echo.
    echo  [OK] Scheduled task removed.
    echo  The daemon will no longer start automatically at login.
    echo.
    echo  Note: if the daemon is currently running, it will keep running
    echo  until you press Ctrl+Shift+F10 or end python.exe in Task Manager.
) else (
    echo.
    echo  [INFO] Task not found — it may already have been removed.
)

echo.
pause
endlocal
