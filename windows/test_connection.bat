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
title SMC ICT Agent - connection test
echo ============================================================
echo   Connection test: can the agent really trade on this account?
echo ============================================================
echo.
echo This places ONE tiny BUY LIMIT order (minimum lot) far below the market
echo on the account MT5 is logged into, checks that the broker accepted it,
echo and cancels it again straight away. It can't fill: no position is opened.
echo You will see it in MT5 under Toolbox - History and Toolbox - Journal.
echo.
set /p ok="Type YES to run the test: "
if /i not "%ok%"=="YES" (
  echo Cancelled.
  pause
  exit /b 0
)
smc-agent -c config.yaml check --test-order
echo.
pause
