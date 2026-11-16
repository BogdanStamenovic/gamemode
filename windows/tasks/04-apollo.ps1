# Install Apollo (Sunshine fork with a virtual display) silently, set its web
# credentials from C:\gamemode\secrets\apollo.json, and report what got installed.
$ErrorActionPreference = 'Continue'
$inst = Get-ChildItem C:\gamemode\dl\Apollo-*.exe | Sort-Object Name | Select-Object -Last 1
$dir = 'C:\Program Files\Apollo'
if (-not (Test-Path "$dir\sunshine.exe")) {
  "installing $($inst.Name)"
  $p = Start-Process $inst.FullName -ArgumentList '/S' -PassThru
  # A silent NSIS installer can still pop a driver dialog nobody will click; give up after 8 min.
  if ($p.WaitForExit(480000)) { "installer exit: $($p.ExitCode)" } else { 'installer still running after 8 min (a dialog?)'; Get-Process | Where-Object { $_.MainWindowTitle } | Format-Table Name, Id, MainWindowTitle -AutoSize | Out-String -Width 200 }
} else { 'Apollo already installed' }
Get-ChildItem $dir -ErrorAction SilentlyContinue | Select-Object Name | Format-Wide -Column 4 | Out-String
$s = Get-Content C:\gamemode\secrets\apollo.json -Raw | ConvertFrom-Json
# Config before creds: stop the service so it does not rewrite the file under us.
$svc = Get-Service | Where-Object { $_.Name -match 'Apollo|Sunshine' }
"service(s): $($svc.Name -join ', ')"
$svc | Stop-Service -Force -ErrorAction SilentlyContinue
$conf = "$dir\config\sunshine.conf"
New-Item -ItemType Directory -Force "$dir\config" | Out-Null
$lines = @()
if (Test-Path $conf) { $lines = Get-Content $conf | Where-Object { $_ -notmatch '^(sunshine_name|origin_web_ui_allowed|min_log_level)\s*=' } }
$lines += 'sunshine_name = archserver'
$lines += 'origin_web_ui_allowed = lan'
$lines += 'min_log_level = info'
Set-Content -Path $conf -Value $lines -Encoding ascii
"--- sunshine.conf"; Get-Content $conf
Push-Location $dir
& "$dir\sunshine.exe" --creds $s.user $s.password 2>&1 | Select-Object -Last 5
Pop-Location
$svc | Set-Service -StartupType Automatic
$svc | Start-Service -ErrorAction SilentlyContinue
Start-Sleep 8
Get-Service | Where-Object { $_.Name -match 'Apollo|Sunshine' } | Format-Table Name, Status, StartType -AutoSize | Out-String
Get-Process | Where-Object { $_.Name -match 'sunshine|apollo' } | Format-Table Name, Id, SessionId -AutoSize | Out-String
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in 47984, 47989, 47990, 48010 } | Format-Table LocalAddress, LocalPort, OwningProcess -AutoSize | Out-String
Get-NetFirewallRule | Where-Object { $_.DisplayName -match 'Apollo|Sunshine' } | Format-Table DisplayName, Enabled, Profile -AutoSize | Out-String
Get-PnpDevice -ErrorAction SilentlyContinue | Where-Object { $_.FriendlyName -match 'SudoVDA|Virtual Display|ViGEm|Virtual Gamepad' } | Format-Table FriendlyName, Status -AutoSize | Out-String
# A web-UI login proves the creds took.
add-type 'using System.Net; using System.Security.Cryptography.X509Certificates; public class TA : ICertificatePolicy { public bool CheckValidationResult(ServicePoint a, X509Certificate b, WebRequest c, int d) { return true; } }'
[System.Net.ServicePointManager]::CertificatePolicy = New-Object TA
[System.Net.ServicePointManager]::SecurityProtocol = 'Tls12'
$pair = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("$($s.user):$($s.password)"))
try { $r = Invoke-WebRequest -UseBasicParsing -Uri 'https://localhost:47990/api/config' -Headers @{ Authorization = "Basic $pair" } -TimeoutSec 10; "api/config: $($r.StatusCode)" } catch { "api/config failed: $_" }
Get-Content "$dir\config\sunshine.log" -Tail 40 -ErrorAction SilentlyContinue
