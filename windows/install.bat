@echo off
setlocal
cd /d "%~dp0\.."
rem Other programs (e.g. ZKBioTime) can set PYTHONHOME / PYTHONPATH for the whole PC, which makes
rem every Python load THEIR library ("SRE module mismatch"). Ignore them here only.
set "PYTHONHOME="
set "PYTHONPATH="
title SMC ICT Agent - install
echo ============================================================
echo   SMC ICT Agent - installation
echo ============================================================
echo.
rem MetaTrader5 only works with 64-bit Python (3.12 recommended).
rem Try every installed Python and keep the first 64-bit one, even if a 32-bit one is on PATH.
set "PY="
for %%C in ("py -3.12-64" "py -3.12" "py -3.11-64" "py -3.11" "py -3.13-64" "py -3.13" "py -3.10-64" "py -3.10" "py -3-64" "py -3" "python") do (
  if not defined PY (
    %%~C -c "import struct,sys,re,venv; sys.exit(0 if struct.calcsize('P') == 8 and sys.version_info[:2] >= (3, 10) else 1)" >nul 2>nul && set "PY=%%~C"
  )
)
if not defined PY goto nopython64
echo Using 64-bit Python: %PY%
%PY% --version

rem an environment made earlier with a 32-bit Python can't load MetaTrader5: rebuild it
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import struct,sys; sys.exit(0 if struct.calcsize('P') == 8 else 1)" >nul 2>nul
  if errorlevel 1 (
    echo Removing the old 32-bit environment...
    rmdir /s /q ".venv"
  )
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
exit /b 0

:nopython64
echo No 64-bit Python 3.10 - 3.13 was found on this PC.
echo MetaTrader5 does not work with 32-bit Python.
echo.
echo   1. Download Python 3.12 64-bit - the download starts now:
echo      https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe
echo   2. Run it, tick "Add python.exe to PATH", then click "Install Now".
echo      You can keep the 32-bit Python, both can be installed side by side.
echo   3. Double-click windows\install.bat again.
echo.
start "" "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
pause
exit /b 1
