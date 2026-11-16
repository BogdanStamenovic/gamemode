# Read-only snapshot of everything gamemode depends on. Linux reads this back.
$ErrorActionPreference = 'Continue'
function H($t) { "`n===== $t =====" }
H 'os'; Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, BuildNumber, LastBootUpTime | Format-List | Out-String
H 'whoami'; whoami
H 'users'; Get-LocalUser | Select-Object Name, Enabled, PrincipalSource, PasswordRequired, LastLogon | Format-Table -AutoSize | Out-String -Width 200
H 'administrators'; Get-LocalGroupMember -SID S-1-5-32-544 | Format-Table -AutoSize | Out-String -Width 200
H 'sessions'; query user 2>&1
H 'net adapters'; Get-NetAdapter | Select-Object Name, InterfaceDescription, Status, LinkSpeed, MacAddress | Format-Table -AutoSize | Out-String -Width 200
H 'ip'; Get-NetIPAddress -AddressFamily IPv4 | Select-Object InterfaceAlias, IPAddress, PrefixOrigin | Format-Table -AutoSize | Out-String -Width 200
H 'net profiles'; Get-NetConnectionProfile | Format-Table Name, InterfaceAlias, NetworkCategory -AutoSize | Out-String -Width 200
H 'wol properties'; Get-NetAdapterAdvancedProperty -Name * -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -match 'Wake|Energy|Green|Power' } | Format-Table Name, DisplayName, DisplayValue -AutoSize | Out-String -Width 200
H 'services'; Get-Service sshd, ssh-agent, Tailscale, gamemoded, *Apollo*, *Sunshine*, vgc -ErrorAction SilentlyContinue | Format-Table Name, Status, StartType -AutoSize | Out-String -Width 200
H 'firewall ssh rules'; Get-NetFirewallRule | Where-Object { $_.DisplayName -match 'ssh|gamemode|Apollo|Sunshine' } | Format-Table DisplayName, Enabled, Profile, Direction, Action -AutoSize | Out-String -Width 200
H 'power'; powercfg /a; powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE | Select-String 'Current AC'
H 'fast startup'; Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' -Name HiberbootEnabled
H 'gpu'; Get-CimInstance Win32_VideoController | Select-Object Name, DriverVersion, DriverDate, VideoModeDescription | Format-List | Out-String
H 'monitors'; Get-CimInstance -Namespace root\wmi WmiMonitorID -ErrorAction SilentlyContinue | ForEach-Object { ($_.UserFriendlyName | Where-Object { $_ } | ForEach-Object { [char]$_ }) -join '' }
H 'fwbootmgr'; bcdedit /enum '{fwbootmgr}'
H 'bootmgr'; bcdedit /enum '{bootmgr}'
H 'defender'; Get-MpComputerStatus | Select-Object AMServiceEnabled, RealTimeProtectionEnabled, IsTamperProtected, AntivirusSignatureLastUpdated | Format-List | Out-String
Get-MpPreference | Select-Object -ExpandProperty ExclusionPath -ErrorAction SilentlyContinue
H 'defender detections'; Get-MpThreatDetection -ErrorAction SilentlyContinue | Select-Object -First 10 | Format-List | Out-String
H 'windows update'; Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -ErrorAction SilentlyContinue | Format-List | Out-String
Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'
H 'uptime boot events'; Get-WinEvent -FilterHashtable @{LogName='System'; Id=12,13,1074,6008} -MaxEvents 10 -ErrorAction SilentlyContinue | Format-Table TimeCreated, Id, Message -AutoSize -Wrap | Out-String -Width 220
H 'gamemoded events'; Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Service Control Manager'} -MaxEvents 40 -ErrorAction SilentlyContinue | Where-Object { $_.Message -match 'gamemode|Apollo|sshd' } | Format-Table TimeCreated, Id, Message -AutoSize -Wrap | Out-String -Width 220
