@echo off
setlocal
cd /d "%~dp0\.."
if not exist "state" mkdir state
type nul > "state\HALT"
echo New trades are PAUSED. Open trades keep their stop and target.
echo Double-click resume_trading.bat to allow new trades again.
pause
