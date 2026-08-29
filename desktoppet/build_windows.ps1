param([switch]$NoInstall)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

if (-not $NoInstall) {
    python -m pip install -r requirements.txt
}

python -m PyInstaller --noconfirm --clean `
    --onefile `
    --windowed `
    --name owaua `
    --add-data "desktoppet.jpg;." `
    pet.py

Write-Host ""
Write-Host "Done: dist\owaua.exe"
