Write-Host "🧩 Tworzenie / aktywacja środowiska Python venv..." -ForegroundColor Cyan
Set-Location $PSScriptRoot

if (-not (Test-Path "venv")) {
    python -m venv venv
}

# Aktywuj środowisko
& "$PSScriptRoot\venv\Scripts\Activate.ps1"

Write-Host "📦 Instalowanie bibliotek (jeśli brak)..." -ForegroundColor Yellow
pip install --upgrade pip
pip install matplotlib gpxpy folium contextily pyproj numpy scipy

Write-Host "🧾 Generowanie requirements.txt..." -ForegroundColor Yellow
pip freeze | Out-File -Encoding utf8 requirements.txt

Write-Host "🚀 Uruchamianie GPXEditor..." -ForegroundColor Green
python main.py
