@echo off
title Hello box
cd /d "%~dp0"

set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo.
  echo  Python is not installed.
  echo  1. Download it from https://www.python.org/downloads/
  echo  2. When installing, TICK the box "Add python.exe to PATH"
  echo  3. Then double-click start.bat again.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo First start: setting things up. This takes a minute...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo Could not create the Python environment.
    pause
    exit /b 1
  )
)

echo Checking required components...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo Could not install the required components. Check your internet connection and try again.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m outreach
pause
