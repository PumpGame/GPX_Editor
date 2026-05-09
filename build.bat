@echo off
REM ==========================================================
REM  GPXEditor — tworzy pakiet wykonawczy przy użyciu PyInstaller
REM ==========================================================

REM Przejdź do folderu, w którym znajduje się ten skrypt
cd /d "%~dp0"

echo.
echo 🧩 [1/4] Tworzenie/aktywacja środowiska virtualenv...
if not exist "venv\" (
    python -m venv venv
)

call "%~dp0venv\Scripts\activate.bat"
echo.
echo ⚙️  [2/4] Instalacja PyInstaller i zależności...
pip install --upgrade pip
pip install pyinstaller

echo.
echo 📦 [3/4] Budowanie aplikacji przy użyciu GPXEditor.spec...
pyinstaller --clean --noconfirm --distpath dist --workpath build "GPXEditor.spec"
echo.
echo ✅ [4/4] Gotowe! Znajdziesz plik EXE w katalogu dist\GPXEditor
echo.
pause
