@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title TZ Checker - local application

echo.
echo =====================================================
echo   TZ Checker - local application for Windows
echo =====================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo ERROR: Python was not found in PATH.
  echo Python 3.11 or newer is required. Ask IT to make Python available.
  pause
  exit /b 1
)

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 (
  echo ERROR: Python 3.11 or newer is required. Current version:
  python -c "import sys; print(sys.version)"
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating a local Python environment in this folder...
  python -m venv .venv
  if errorlevel 1 (
    echo ERROR: Could not create the local virtual environment.
    pause
    exit /b 1
  )
)

if exist ".venv\requirements.installed" (
  fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1
  if not errorlevel 1 goto run
)

echo Installing or updating required packages (Internet access to PyPI is required).
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo.
  echo ERROR: Package installation failed.
  echo Ask IT to approve user-local Python packages or provide an approved package source.
  pause
  exit /b 1
)
copy /y requirements.txt ".venv\requirements.installed" >nul

:run
if "%TZ_CHECKER_PORT%"=="" set TZ_CHECKER_PORT=8000
echo.
echo Starting the local server on http://127.0.0.1:%TZ_CHECKER_PORT%
echo To stop the server press Ctrl+C in this window.
start "" /B cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:%TZ_CHECKER_PORT%"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port %TZ_CHECKER_PORT%

echo.
echo The local server has stopped.
pause
