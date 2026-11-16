$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
python service_control.py
if ($LASTEXITCODE -ne 0) { throw 'ANPR startup failed. Open run.bat.' }
