@echo off
chcp 65001 >nul
rem Сборка FTX1-Pult.exe — один файл, без консоли, Python на целевом ПК не нужен.
cd /d "%~dp0"
where python >nul 2>nul || (echo Нужен Python 3.10+ с python.org & pause & exit /b 1)
if not exist ".venv\Scripts\python.exe" python -m venv .venv || goto :err
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller || goto :err
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed --name FTX1-Pult ^
  --icon web\icon.ico --add-data "web;web" --add-data "server\menu_ftx1.json;." ^
  --hidden-import pystray._win32 --paths server server\main.py || goto :err
echo.
echo Готово: dist\FTX1-Pult.exe
echo Положите его в любую папку — config.json и журнал появятся рядом.
pause
exit /b 0
:err
echo Ошибка сборки.
pause
exit /b 1
