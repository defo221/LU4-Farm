@echo off
cd /d "%~dp0"
python minimap_orient.py --mode align %*
pause
