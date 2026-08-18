@echo off
:: Main-PC fleet viewer. Reads the host list from stream_slaves.json.
::
:: To bypass the config file for a quick test against one or two slaves:
::   run_stream_viewer.bat --slaves 192.168.0.11:8772,192.168.0.12:8772

cd /d "%~dp0"
python stream_viewer.py %*
pause
