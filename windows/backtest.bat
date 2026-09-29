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
title SMC ICT Agent - backtest on your broker's history
if not exist "state" mkdir state
echo Replaying your broker's recent history through the agent (no orders are sent)...
smc-agent -c config.yaml backtest --bars 20000 --chart state\backtest.html --trades state\backtest_trades.csv
if exist "state\backtest.html" start "" "state\backtest.html"
echo.
echo Trades: state\backtest_trades.csv
pause
