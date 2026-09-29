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
title SMC ICT Agent - settings
echo ============================================================
echo   SMC ICT Agent - settings
echo ============================================================
echo.
echo Current settings:
smc-agent -c config.yaml mode
if errorlevel 1 (
  echo.
  echo config.yaml has a problem - see the message above.
  pause
  exit /b 1
)
echo.
echo ------------------------------------------------------------
echo Where should the trades go?
echo   1 = PAPER  simulated only, nothing is sent to MT5
echo   2 = MT5    real orders on the account MT5 is logged into
echo              (use a DEMO account first)
set "B="
set /p B="Type 1 or 2 and press Enter (just Enter = keep): "
echo.
echo How many trades do you want?
echo   1 = safe      fewest trades, strictest filters
echo   2 = balanced  about 40%% more trades, same win rate in tests
echo   3 = active    about 3x more trades, lower win rate, deeper drawdowns
set "P="
set /p P="Type 1, 2 or 3 and press Enter (just Enter = keep): "
echo.
echo Which chart timeframe should it trade?
echo   1 = M15  fewer, bigger setups
echo   2 = M5   about 3x more setups (same rules on smaller candles)
set "T="
set /p T="Type 1 or 2 and press Enter (just Enter = keep): "
echo.
echo More markets = more trades at the same quality. Type the EXACT Market Watch
echo names to add, separated by spaces (e.g.  XAGUSD_ EURUSD_ GBPUSD_ US30_ )
set "M="
set /p M="Markets to add (just Enter = none): "
set "ARGS="
if "%B%"=="1" set "ARGS=%ARGS% --broker paper"
if "%B%"=="2" set "ARGS=%ARGS% --broker mt5"
if "%P%"=="1" set "ARGS=%ARGS% --profile safe"
if "%P%"=="2" set "ARGS=%ARGS% --profile balanced"
if "%P%"=="3" set "ARGS=%ARGS% --profile active"
if "%T%"=="1" set "ARGS=%ARGS% --timeframe 15m"
if "%T%"=="2" set "ARGS=%ARGS% --timeframe 5m"
if defined M set "ARGS=%ARGS% --add-market "%M%""
echo.
if not defined ARGS (
  echo Nothing changed.
  pause
  exit /b 0
)
smc-agent -c config.yaml mode %ARGS%
echo.
if "%B%"=="2" echo Tip: run test_connection.bat once to prove the agent can trade on this account.
if defined M echo Run check.bat to confirm the new market names exist at your broker.
echo Close the start_agent window if it is running, then double-click start_agent.bat again.
pause
