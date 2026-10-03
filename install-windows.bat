@echo off
setlocal
cd /d "%~dp0"
call run-source.bat -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 goto fail
if not exist ".venv\Scripts\python.exe" (
    call run-source.bat -m venv .venv
    if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -c "import tkinter; from zoneinfo import ZoneInfo; ZoneInfo('Australia/Melbourne')"
if errorlevel 1 goto fail
echo Installation complete. Run run-widget.bat to configure your own calendar feed.
pause
exit /b 0
:fail
echo Installation failed. Check the error above. Python 3.10+ with Tk is required.
pause
exit /b 1
