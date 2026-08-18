# One-time firewall setup for a slave PC.
# stream_sender.py listens on TCP 8772. The first time Python binds that port,
# Windows may create a hidden BLOCK rule for python.exe. Block rules beat allow
# rules, so the port stay closed until those python rules are deleted.
#
# Run elevated. setup_slave_firewall.bat does that for you.

$ErrorActionPreference = "Continue"

Write-Host ""
Write-Host "=== 1. Existing Python firewall rules (look for Action: Block) ==="
Get-NetFirewallApplicationFilter |
  Where-Object { $_.Program -like "*python*" } |
  ForEach-Object { Get-NetFirewallRule -AssociatedNetFirewallApplicationFilter $_ } |
  Select-Object DisplayName, Direction, Action, Profile, Enabled |
  Format-Table -AutoSize

Write-Host "=== 2. Deleting every Python firewall rule ==="
$pyRules = Get-NetFirewallApplicationFilter |
  Where-Object { $_.Program -like "*python*" } |
  ForEach-Object { Get-NetFirewallRule -AssociatedNetFirewallApplicationFilter $_ }
if ($pyRules) {
  $pyRules | ForEach-Object {
    Write-Host ("  removing: " + $_.DisplayName + "  [" + $_.Action + " / " + $_.Profile + "]")
  }
  $pyRules | Remove-NetFirewallRule -ErrorAction SilentlyContinue
  Write-Host "  done."
} else {
  Write-Host "  none found."
}

Write-Host ""
Write-Host "=== 3. Allow inbound TCP 8772 on all profiles ==="
Remove-NetFirewallRule -DisplayName "PXM stream sender" -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName "PXM stream sender" -Direction Inbound `
  -Action Allow -Protocol TCP -LocalPort 8772 -Profile Any | Out-Null
Get-NetFirewallRule -DisplayName "PXM stream sender" |
  Select-Object DisplayName, Direction, Action, Profile, Enabled |
  Format-Table -AutoSize

Write-Host "=== 4. This PC's network profile and address ==="
Get-NetConnectionProfile | ForEach-Object {
  Write-Host ("  " + $_.InterfaceAlias + " = " + $_.NetworkCategory)
}
Get-NetIPAddress -AddressFamily IPv4 |
  Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
  ForEach-Object {
    Write-Host ("  " + $_.IPAddress + "   (" + $_.InterfaceAlias + ")")
  }
Write-Host ""
Write-Host "Put that address into stream_slaves.json on the main PC."
