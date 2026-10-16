$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
python monitor.py --port 8765
