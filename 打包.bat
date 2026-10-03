@echo off
cd /d "%~dp0"
call run-source.bat -m PyInstaller --noconfirm --onefile --noconsole --name CanvasDeadlines --add-data "ui.html;." --collect-all tzdata --collect-all certifi --workpath build --specpath build --distpath dist app.py
set "RESULT=%errorlevel%"
pause
exit /b %RESULT%
