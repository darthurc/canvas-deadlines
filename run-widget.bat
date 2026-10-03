@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" app.py widget %*
    exit /b 0
)
call run-source.bat app.py widget %*
exit /b %errorlevel%
