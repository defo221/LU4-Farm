@echo off
:: Run on the DEV machine after making any changes.
:: Generates manifest.json so other machines can detect what needs updating.
cd /d "%~dp0"
python updater.py --make
echo.
pause
