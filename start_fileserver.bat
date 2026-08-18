@echo off
:: Starts the HTTP update server that serves C:\PXM_LU4 to all other machines.
:: Run this on the DEV machine AFTER running make_manifest.bat.
::
:: Other machines connect to:  http://192.168.0.156:8081
:: The server prints its own LAN address on startup, so no ipconfig needed.
::
:: Only files listed in manifest.json are served. Secrets elsewhere in the tree
:: (PXM_RB\accounts.json, the *_local.py tokens, logs\) are never reachable.
::
:: Open http://192.168.0.156:8081 in a browser to see exactly what is being served.
::
:: If the slaves cannot connect, allow the port through the firewall once,
:: from an elevated prompt:
::   netsh advfirewall firewall add rule name="PXM_LU4 update server" ^
::         dir=in action=allow protocol=TCP localport=8081
::
:: The server runs in this window - close the window (or Ctrl+C) to stop it.

cd /d "%~dp0"
python fileserver.py --port 8081
pause
