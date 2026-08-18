@echo off
:: Run on any OTHER machine to check and download updates from the DEV machine.
::
:: The DEV machine (192.168.0.156) must be running start_server.bat first.
::
:: You can also point at a USB drive or network share instead:
::   set SOURCE=E:\PXM_LU4
::   set SOURCE=\\192.168.0.156\PXM_LU4
::
cd /d "%~dp0"
set SOURCE=http://192.168.0.156:8081

python updater.py --source "%SOURCE%"
pause
