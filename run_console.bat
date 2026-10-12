@echo off
:: run_console.bat
:: Launches the OCR daemon WITH a visible console window.
:: Use this for testing and to watch live log output.
::
:: FULLSCREEN WARNING
:: This console window takes foreground focus when it opens. If GSPro is
:: already running in EXCLUSIVE FULLSCREEN, that focus change will drop it
:: to windowed mode and shift every capture region.
::
:: Avoid this by either:
::   1. Starting this daemon BEFORE launching GSPro, or
::   2. Setting GSPro to Borderless Windowed (recommended - it cannot
::      mode-switch at all), or
::   3. Using run_background.vbs, which opens no window.
::
:: Press Ctrl+Shift+F10 to stop the daemon gracefully.

cd /d "%~dp0"
python ocr_daemon.py %*
pause
