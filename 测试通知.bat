@echo off
chcp 65001 >nul
cd /d "%~dp0"
call run-source.bat notify.py
set "RESULT=%errorlevel%"
pause
exit /b %RESULT%
