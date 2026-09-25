@echo off
setlocal
cd /d "%~dp0\.."
rem Other programs (e.g. ZKBioTime) can set PYTHONHOME / PYTHONPATH for the whole PC, which makes
rem every Python load THEIR library ("SRE module mismatch"). Ignore them here only.
set "PYTHONHOME="
set "PYTHONPATH="
if not exist ".venv\Scripts\activate.bat" (
  echo The bot is not installed yet. Double-click windows\install.bat first.
  pause
  exit /b 1
)
call ".venv\Scripts\activate.bat"
if exist "windows\secrets.bat" call "windows\secrets.bat"
title SMC ICT Agent - TradingView webhook RUNNING
:loop
smc-agent -c config.yaml webhook
echo Restarting in 30 seconds - close this window to stop.
timeout /t 30
goto loop
