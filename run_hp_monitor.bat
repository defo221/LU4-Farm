@echo off
:: HP Monitor - reads mob HP bar from screen once per second.
:: Logs to console and to logs\pxm_*.log
::
:: Usage:
::   run_hp_monitor.bat           normal mode
::   run_hp_monitor.bat --debug   saves debug_hp_region.png each tick
::                                to help calibrate HP_BAR_OFFSET_* values

cd /d "%~dp0"
python hp_monitor.py %*
pause
