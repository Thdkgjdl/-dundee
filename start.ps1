$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
if (Test-Path -LiteralPath '.venv\Scripts\python.exe') {
    & '.\.venv\Scripts\python.exe' .\app.py
} else {
    python .\app.py
}
