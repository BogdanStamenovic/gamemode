# /mnt/offload is an ext4 image on this NTFS volume: Windows must never leave
# the volume hibernated or fast-started. Also: never sleep on AC (an idle
# Windows must stay reachable until gamemoded restarts it), and arm Wake-on-LAN.
$ErrorActionPreference = 'Continue'
powercfg /hibernate off
Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' -Name HiberbootEnabled -Value 0 -Type DWord
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 10
foreach ($a in Get-NetAdapter -Physical | Where-Object { $_.InterfaceDescription -match 'Realtek|Ethernet|GbE' }) {
  "adapter: $($a.Name) $($a.InterfaceDescription)"
  foreach ($kw in '*WakeOnMagicPacket', '*WakeOnPattern', 'S5WakeOnLan', 'WakeOnLink') {
    $p = Get-NetAdapterAdvancedProperty -Name $a.Name -RegistryKeyword $kw -ErrorAction SilentlyContinue
    if ($p) {
      $want = if ($kw -eq '*WakeOnPattern' -or $kw -eq 'WakeOnLink') { $p.RegistryValue } else { '1' }
      Set-NetAdapterAdvancedProperty -Name $a.Name -RegistryKeyword $kw -RegistryValue $want -ErrorAction SilentlyContinue
      "  $kw -> $((Get-NetAdapterAdvancedProperty -Name $a.Name -RegistryKeyword $kw).DisplayValue)"
    }
  }
  # "Shutdown Wake-On-Lan" and the power-management tab: let the device wake the machine.
  Set-NetAdapterPowerManagement -Name $a.Name -WakeOnMagicPacket Enabled -ErrorAction SilentlyContinue
  Get-NetAdapterPowerManagement -Name $a.Name | Format-List WakeOnMagicPacket, AllowComputerToTurnOffDevice | Out-String
}
powercfg /a
powercfg /devicequery wake_armed
