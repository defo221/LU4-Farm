@echo off
:: Slave-side screen streamer + Arduino HID bridge.
::
:: With no arguments it uses this PC's hostname as the tile label, listens on
:: 8772, and auto-detects the Arduino COM port. Override any of those:
::   run_stream_sender.bat --name PC4 --port 8772 --arduino COM12
::
:: 8772 is clear of the other fleet services: 8770 PXM_RB coordinator API,
:: 8081 PXM_LU4 update server, 8000 PXM_RB file server.
::
:: The Arduino COM port is opened exclusively. If bot.py or PXM_RB's agent is
:: already running on this PC and holds it, this process streams video only and
:: the tile is labelled NO ARDUINO. Stop the other one first to take control.
::
:: No elevation needed: screen capture and cursor reads work unprivileged, and
:: the input itself comes from the Arduino, not from this process.
::
:: The target game window must stay expanded - a minimized window captures black.

cd /d "%~dp0"
python stream_sender.py %*
pause
