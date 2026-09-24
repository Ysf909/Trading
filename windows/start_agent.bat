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
title SMC ICT Agent - RUNNING (close this window to stop)
if exist "state\HALT" (
  echo NOTE: state\HALT exists - the agent will manage open trades but take no new ones.
  echo       Double-click windows\resume_trading.bat to allow new trades.
  echo.
)
:loop
echo [%date% %time%] starting the agent...
smc-agent -c config.yaml run
echo.
echo [%date% %time%] the agent stopped ^(exit code %errorlevel%^). Restarting in 30 seconds - close this window to stop.
timeout /t 30
goto loop
