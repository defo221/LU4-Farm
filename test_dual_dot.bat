@echo off
:: test_dual_dot.bat  -  find both in_target_blue dots and show their midpoint.
::
:: Usage:
::   test_dual_dot.bat                          live screenshot, default confidence
::   test_dual_dot.bat --conf 0.70              lower threshold if dots are missed
::   test_dual_dot.bat --image screenshot.png   test against a saved image
::   test_dual_dot.bat --tmpl assets\fullhd\in_target_blue.png   different template
::
:: The script detects all blue target dots on screen, groups them into left/right
:: pairs, and draws a red crosshair at the exact center between each pair.

cd /d "%~dp0"
python test_dual_dot.py %*
pause
