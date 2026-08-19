@echo off
:: Transparent fullscreen overlay — red dot between detected in_target_blue pairs.
:: Press Esc to close the overlay.
::
:: Options:
::   --conf 0.70   lower threshold if dots are missed (default 0.75)
::   --hz 15       lower capture rate if CPU load is too high (default 20)

cd /d "%~dp0"
python test_dual_dot_overlay.py %*
pause
