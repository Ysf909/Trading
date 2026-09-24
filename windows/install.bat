@echo off
setlocal
cd /d "%~dp0\.."
title SMC ICT Agent - install
echo ============================================================
echo   SMC ICT Agent - installation
echo ============================================================
echo.
set "PY="
py -3.12 --version >nul 2>nul && set "PY=py -3.12"
if not defined PY py -3.11 --version >nul 2>nul && set "PY=py -3.11"
if not defined PY py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python was not found.
  echo Install Python 3.12 64-bit from https://www.python.org/downloads/windows/
  echo and tick "Add python.exe to PATH" on the first screen, then run this again.
  pause
  exit /b 1
)
echo Using: %PY%
%PY% -c "import struct,sys; sys.exit(0 if struct.calcsize('P') == 8 else 1)"
if errorlevel 1 (
  echo This Python is 32-bit. MetaTrader5 needs 64-bit Python 3.12.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Creating the virtual environment...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo Could not create the virtual environment.
    pause
    exit /b 1
  )
)
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
python -m pip install -e ".[mt5,ai,chart]"
if errorlevel 1 (
  echo.
  echo Installation failed. If the error mentions MetaTrader5, install Python 3.12 64-bit
  echo ^(the MetaTrader5 package does not support every Python version^) and run this again.
  pause
  exit /b 1
)
if not exist "config.yaml" (
  copy /y "config.example.yaml" "config.yaml" >nul
  echo Created config.yaml from config.example.yaml
)
if not exist "windows\secrets.bat" (
  copy /y "windows\secrets.example.bat" "windows\secrets.bat" >nul
  echo Created windows\secrets.bat for your Telegram / API keys
)
if not exist "state" mkdir state
echo.
echo ============================================================
echo   Installed.
echo   1. Edit config.yaml ^(symbol name, risk^) - opening it now
echo   2. Double-click windows\check.bat and fix everything it reports
echo ============================================================
start "" notepad "config.yaml"
pause
