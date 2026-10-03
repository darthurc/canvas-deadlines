$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectPath '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $pythonPath) {
    & $pythonPath (Join-Path $projectPath 'autostart.py') uninstall
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 (Join-Path $projectPath 'autostart.py') uninstall
} else {
    & python (Join-Path $projectPath 'autostart.py') uninstall
}
exit $LASTEXITCODE
