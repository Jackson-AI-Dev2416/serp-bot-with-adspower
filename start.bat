@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul

if not exist "data" mkdir "data"

if not exist ".venv\Scripts\python.exe" (
  echo 가상환경이 없습니다. setup.bat 을 먼저 실행합니다...
  call "%~dp0setup.bat"
  if errorlevel 1 exit /b 1
)

".venv\Scripts\python.exe" -c "import PyQt6" >nul 2>&1
if errorlevel 1 (
  echo 필요한 모듈이 없습니다. setup.bat 을 실행합니다...
  call "%~dp0setup.bat"
  if errorlevel 1 exit /b 1
)

".venv\Scripts\python.exe" main.py
if errorlevel 1 (
  echo.
  echo SERP Bot 실행 중 오류가 발생했습니다.
  pause
)
