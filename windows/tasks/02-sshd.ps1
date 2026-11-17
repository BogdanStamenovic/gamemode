# sshd is the relay's way in when gamemoded itself is not running. The stock
# firewall rule only covers the Private profile, and the wired LAN may be Public.
$ErrorActionPreference = 'Continue'
Set-Service sshd -StartupType Automatic
Start-Service sshd
if (-not (Get-NetFirewallRule -Name gamemode-sshd -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -Name gamemode-sshd -DisplayName 'gamemode: sshd from LAN' -Direction Inbound -Protocol TCP -LocalPort 22 -RemoteAddress LocalSubnet, 100.64.0.0/10 -Action Allow -Profile Any | Out-Null
}
# Windows drops ping on the Public profile, which is what the wired LAN is here;
# the laptop's latency measurement needs echo replies from the LAN.
if (-not (Get-NetFirewallRule -Name gamemode-icmp -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -Name gamemode-icmp -DisplayName 'gamemode: ping from LAN' -Direction Inbound -Protocol ICMPv4 -IcmpType 8 -RemoteAddress LocalSubnet -Action Allow -Profile Any | Out-Null
}
Get-NetFirewallRule -Name gamemode-icmp | Format-List Name, Enabled, Profile | Out-String
Get-Service sshd | Format-List Name, Status, StartType | Out-String
Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue | Format-Table LocalAddress, LocalPort -AutoSize | Out-String
Get-Content C:\ProgramData\ssh\logs\sshd.log -Tail 20 -ErrorAction SilentlyContinue
