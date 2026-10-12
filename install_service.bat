@echo off
:: install_service.bat
:: Registers the OCR Daemon in Windows Task Scheduler so it starts
:: automatically every time you log in — no manual launch needed.
::
:: Run this once from the folder containing the OCR scripts.
:: Does NOT require Administrator rights.

setlocal
set "TASK_NAME=OCR Daemon"
set "VBS=%~dp0run_background.vbs"

echo.
echo  Registering "%TASK_NAME%" in Task Scheduler...
echo  Script : %VBS%
echo.

schtasks /create ^
    /tn "%TASK_NAME%" ^
    /tr "wscript.exe \"%VBS%\"" ^
    /sc ONLOGON ^
    /ru "%USERDOMAIN%\%USERNAME%" ^
    /f

if %errorlevel%==0 (
    echo.
    echo  [OK] Scheduled task created successfully.
    echo.
    echo  The daemon will now start automatically at every login.
    echo.
    echo  To start it RIGHT NOW without logging out:
    echo    Double-click  run_background.vbs
    echo.
    echo  To stop the daemon:
    echo    Press Ctrl+Shift+F10  ^(in any window^)
    echo    — or kill python.exe in Task Manager
    echo.
    echo  To remove auto-start:
    echo    Run  uninstall_service.bat
) else (
    echo.
    echo  [ERROR] Could not create the scheduled task.
    echo  Try right-clicking install_service.bat and choosing
    echo  "Run as administrator".
)

echo.
pause
endlocal
