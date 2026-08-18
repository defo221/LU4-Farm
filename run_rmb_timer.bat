@echo off
cd /d "%~dp0"
echo Starting RMB anchor timer...
echo If right-clicks are not detected, run this file as Administrator.
echo.
python rmb_anchor_timer.py
pause
