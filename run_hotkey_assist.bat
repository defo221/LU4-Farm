@echo off
:: Hotkey Assist launcher — runs elevated automatically.
:: Configure HOTKEY_* and HOTKEY_WIN*_ASSIST in config.py before running.

:: Re-launch with UAC elevation if not already admin
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"
python hotkey_assist.py %*
pause
