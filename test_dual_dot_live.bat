@echo off
:: Real-time red dot at the center between detected in_target_blue pairs.
:: Press Q or Esc in the preview window to stop.
::
:: Options:
::   --title "Lineage"   substring of the game window title (default "Lineage")
::   --conf 0.70         lower threshold if dots are missed (default 0.75)
::   --tmpl assets\fullhd\in_target_blue.png   use a different template

cd /d "%~dp0"
python test_dual_dot_live.py %*
pause
