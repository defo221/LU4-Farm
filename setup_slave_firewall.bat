@echo off
:: One-time firewall setup for a SLAVE PC.
:: Removes hidden python.exe BLOCK rules, then allows inbound TCP 8772.
:: Self-elevates. Double-click it, or it is called automatically from
:: run_stream_sender.bat the first time the allow rule is missing.
::
::   setup_slave_firewall.bat          interactive, pauses at the end
::   setup_slave_firewall.bat silent   no pause (used by the sender launcher)

setlocal
set "SILENT=%~1"
set "PS1=%~dp0setup_slave_firewall.ps1"

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%SILENT%' -Verb RunAs -Wait"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"

if /i not "%SILENT%"=="silent" pause
endlocal
