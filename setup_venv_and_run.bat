@echo off
REM ==========================================================
REM  GPXEditor — automatyczne tworzenie środowiska i uruchomienie
REM ==========================================================

REM Przejdź do folderu, w którym znajduje się ten skrypt
cd /d "%~dp0"

echo.
echo 🧩 [1/5] Tworzenie środowiska virtualenv...
if not exist "venv\" (
    python -m venv venv
)

echo.
echo ⚙️  [2/5] Aktywacja środowiska...
call "%~dp0venv\Scripts\activate.bat"

echo.
echo 📦 [3/5] Instalacja bibliotek...
pip install --upgrade pip
pip install matplotlib gpxpy folium contextily pyproj numpy scipy

echo.
echo 🧾 [4/5] Tworzenie pliku requirements.txt...
pip freeze > requirements.txt

echo.
echo 🚀 [5/5] Uruchamianie GPXEditor...
python main.py

echo.
pause
