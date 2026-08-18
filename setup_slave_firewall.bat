@echo off
:: One-time firewall setup for a SLAVE PC, so the main PC's viewer can reach
:: stream_sender.py on port 8772. Runs elevated automatically.
::
:: Why this is needed: the first time Python binds a listening port, Windows
:: shows the "Firewall has blocked some features of this app" dialog. If that
:: dialog is dismissed or cancelled, Windows silently creates BLOCK rules for
:: python.exe. Block rules take precedence over allow rules, so adding a port
:: rule alone does nothing until the block rules are removed. This script
:: removes them, then adds the port rule for every network profile.
::
:: Run this ONCE per slave, ideally BEFORE the first run of stream_sender.bat.

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator rights...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

echo.
echo === Removing any python.exe BLOCK rules =========================
powershell -NoProfile -Command "$r = Get-NetFirewallApplicationFilter | Where-Object { $_.Program -like '*python*' } | ForEach-Object { Get-NetFirewallRule -AssociatedNetFirewallApplicationFilter $_ } | Where-Object { $_.Action -eq 'Block' }; if ($r) { $r | ForEach-Object { Write-Host ('  removing: ' + $_.DisplayName + '  [' + $_.Profile + ']') }; $r | Remove-NetFirewallRule -ErrorAction SilentlyContinue; Write-Host '  done.' } else { Write-Host '  none found (good).' }"

echo.
echo === Adding inbound allow rule for TCP 8772 ======================
powershell -NoProfile -Command "Remove-NetFirewallRule -DisplayName 'PXM stream sender' -ErrorAction SilentlyContinue; New-NetFirewallRule -DisplayName 'PXM stream sender' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8772 -Profile Any | Out-Null; Write-Host '  rule created for all profiles.'"

echo.
echo === Result =====================================================
powershell -NoProfile -Command "Get-NetFirewallRule -DisplayName 'PXM stream sender' | Select-Object DisplayName, Direction, Action, Profile, Enabled | Format-Table -AutoSize"
powershell -NoProfile -Command "Write-Host '  network profile(s):'; Get-NetConnectionProfile | ForEach-Object { Write-Host ('    ' + $_.InterfaceAlias + ' = ' + $_.NetworkCategory) }"
powershell -NoProfile -Command "Write-Host ''; Write-Host '  this PC is:'; Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | ForEach-Object { Write-Host ('    ' + $_.IPAddress + '   (' + $_.InterfaceAlias + ')') }"

echo.
echo Put the address shown above into stream_slaves.json on the MAIN PC.
echo.
pause
