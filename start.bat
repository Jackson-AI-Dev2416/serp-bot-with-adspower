@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist "data" mkdir "data"

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" main.py
  goto :done
)

if exist "venv\Scripts\python.exe" (
  "venv\Scripts\python.exe" main.py
  goto :done
)

where python >nul 2>&1
if errorlevel 1 (
  echo Python was not found. Install Python 3.11+ or create a local venv first.
  pause
  exit /b 1
)

python main.py

:done
if errorlevel 1 (
  echo.
  echo SERP Bot exited with an error.
  pause
)
