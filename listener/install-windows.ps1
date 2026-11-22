# Jarvis listener for Windows: say "Hey Jarvis" to the PC's microphone.
# Needs Python 3.10 or newer (python.org, tick "Add to PATH"). Run in PowerShell:
#   powershell -ExecutionPolicy Bypass -File install-windows.ps1 [-Dir D:\jarvis-listener]
param([string]$Dir = "$env:USERPROFILE\jarvis-listener")
$ErrorActionPreference = "Stop"
$Repo = "https://raw.githubusercontent.com/gtzapper/jarvis/main/listener"

$py = (Get-Command py -ErrorAction SilentlyContinue)
if ($py) { $python = "py"; $pyArgs = @("-3") } else { $python = "python"; $pyArgs = @() }
& $python @pyArgs -c "import sys; assert sys.version_info >= (3, 10), sys.version" 2>$null
if ($LASTEXITCODE -ne 0) { throw "Python 3.10 or newer is needed. Install it from python.org (tick 'Add python.exe to PATH')." }

New-Item -ItemType Directory -Force $Dir | Out-Null
Set-Location $Dir
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
foreach ($f in "jarvis_listener.py", "requirements.txt") {
    $src = if ($here) { Join-Path $here $f } else { "" }
    if ($src -and ($here -ne $Dir) -and (Test-Path $src)) { Copy-Item $src $f -Force }   # running from a copy of the repo
    else { Invoke-WebRequest "$Repo/$f" -OutFile $f -UseBasicParsing }                  # otherwise the latest from GitHub
}

Write-Host "== Python packages"
& $python @pyArgs -m venv venv
& .\venv\Scripts\python.exe -m pip install -q --upgrade pip
& .\venv\Scripts\python.exe -m pip install -q -r requirements.txt
& .\venv\Scripts\python.exe -m pip install -q --no-deps openwakeword

Write-Host "== Setup"
$env:JARVIS_LISTENER_CONFIG = Join-Path $Dir "jarvis-listener.json"
& .\venv\Scripts\python.exe jarvis_listener.py setup

# Start at sign-in, without a console window.
$startup = [Environment]::GetFolderPath("Startup")
$cmd = Join-Path $startup "Jarvis listener.cmd"
Set-Content -Path $cmd -Encoding ascii -Value "@echo off`r`nset JARVIS_LISTENER_CONFIG=$($env:JARVIS_LISTENER_CONFIG)`r`nstart `"`" /min `"$Dir\venv\Scripts\pythonw.exe`" `"$Dir\jarvis_listener.py`""
Write-Host ""
Write-Host "Done. It starts when you sign in. To start it now (and see what it hears):"
Write-Host "  `$env:JARVIS_LISTENER_CONFIG=`"$($env:JARVIS_LISTENER_CONFIG)`"; & `"$Dir\venv\Scripts\python.exe`" `"$Dir\jarvis_listener.py`""
