@echo off
:: DD Farming Bot launcher — runs elevated automatically.
:: Edit config.py before running.

:: Re-launch with UAC elevation if not already admin
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"
python bot.py %*
pause
