@echo off
setlocal
cd /d "%~dp0\.."
echo This cancels every pending order and CLOSES every position of the agent at market,
echo then pauses new trades.
set /p ok="Type YES to continue: "
if /i not "%ok%"=="YES" (
  echo Cancelled.
  pause
  exit /b 0
)
if not exist "state" mkdir state
type nul > "state\FLATTEN"
echo Done: the running agent closes everything within a few seconds.
echo ^(start_agent.bat must be running.^) Double-click resume_trading.bat to trade again later.
pause
