@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" goto venv
where py >nul 2>nul
if not errorlevel 1 goto launcher
where python >nul 2>nul
if not errorlevel 1 goto system
echo Python was not found. Install Python 3.10+ from python.org, then run install-windows.bat.
exit /b 1
:venv
"%~dp0.venv\Scripts\python.exe" %*
exit /b %errorlevel%
:launcher
py -3 %*
exit /b %errorlevel%
:system
python %*
exit /b %errorlevel%
