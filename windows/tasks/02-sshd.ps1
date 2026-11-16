# sshd is the relay's way in when gamemoded itself is not running. The stock
# firewall rule only covers the Private profile, and the wired LAN may be Public.
$ErrorActionPreference = 'Continue'
Set-Service sshd -StartupType Automatic
Start-Service sshd
if (-not (Get-NetFirewallRule -Name gamemode-sshd -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -Name gamemode-sshd -DisplayName 'gamemode: sshd from LAN' -Direction Inbound -Protocol TCP -LocalPort 22 -RemoteAddress LocalSubnet, 100.64.0.0/10 -Action Allow -Profile Any | Out-Null
}
Get-Service sshd | Format-List Name, Status, StartType | Out-String
Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue | Format-Table LocalAddress, LocalPort -AutoSize | Out-String
Get-Content C:\ProgramData\ssh\logs\sshd.log -Tail 20 -ErrorAction SilentlyContinue
