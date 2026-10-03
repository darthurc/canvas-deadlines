@echo off
chcp 65001 >nul
cd /d "%~dp0"
call run-source.bat app.py check %*
set "RESULT=%errorlevel%"
pause
exit /b %RESULT%
