@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul

echo ========================================
echo  SERP Bot - 환경 설정 (최초 1회)
echo ========================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo [오류] Python이 없습니다.
  echo Python 3.11 이상을 설치하고 "Add python.exe to PATH"를 체크하세요.
  echo https://www.python.org/downloads/
  pause
  exit /b 1
)

python --version
echo.

if not exist ".venv\Scripts\python.exe" (
  echo 가상환경 생성 중...
  python -m venv .venv
  if errorlevel 1 (
    echo [오류] venv 생성 실패
    pause
    exit /b 1
  )
)

echo 패키지 설치 중...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [오류] 패키지 설치 실패
  pause
  exit /b 1
)

if not exist "data" mkdir "data"

echo.
echo ========================================
echo  설정 완료. start.bat 로 실행하세요.
echo ========================================
pause
