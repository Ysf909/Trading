@echo off
setlocal
cd /d "%~dp0\.."
if exist "state\HALT" del "state\HALT"
echo New trades are ALLOWED again.
pause
