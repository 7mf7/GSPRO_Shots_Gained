@echo off
:: run_background.bat
:: Launches the OCR daemon with NO console window (pythonw.exe).
:: The process runs completely in the background — check ocr_daemon.log for output.
:: To stop it: press Ctrl+Shift+F10  OR  open Task Manager and end pythonw.exe

cd /d "%~dp0"
start "" /b pythonw ocr_daemon.py
echo OCR Daemon started in background.
echo Log: %~dp0ocr_daemon.log
echo To stop: press Ctrl+Shift+F10, or kill pythonw.exe in Task Manager.
timeout /t 3 >nul
