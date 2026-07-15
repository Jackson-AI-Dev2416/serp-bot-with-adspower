@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
  set "PY=venv\Scripts\python.exe"
) else (
  set "PY=python"
)

"%PY%" -m pip install --upgrade pip pyinstaller
"%PY%" -m PyInstaller build.spec --noconfirm

if exist "dist\SERPBot.exe" (
  echo.
  echo Build complete: dist\SERPBot.exe
  echo Run dist\SERPBot.exe from this project folder so data\ settings resolve correctly.
) else (
  echo Build failed.
  pause
  exit /b 1
)

pause
