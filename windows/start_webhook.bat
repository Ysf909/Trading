@echo off
setlocal
cd /d "%~dp0\.."
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
