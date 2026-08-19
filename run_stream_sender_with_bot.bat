@echo off
:: Slave-side screen streamer + Arduino HID bridge + FarmBot in one process.
::
:: Identical to run_stream_sender.bat but passes --run-bot so the farming bot
:: starts inside the same process and shares the Arduino port automatically.
:: Do NOT run bot.py separately when using this — they would fight over the
:: Arduino COM port.

cd /d "%~dp0"

netsh advfirewall firewall show rule name="PXM stream sender" >nul 2>&1
if errorlevel 1 (
    echo First run: opening firewall for TCP 8772. Approve the UAC prompt.
    call "%~dp0setup_slave_firewall.bat" silent
)

python -c "import dxcam" 2>nul || python -m pip install --quiet --disable-pip-version-check dxcam
python stream_sender.py --run-bot %*
pause
