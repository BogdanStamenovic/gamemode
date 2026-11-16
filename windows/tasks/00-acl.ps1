# C:\gamemode holds a binary that runs as SYSTEM. Files created from Linux get
# whatever ACL ntfs3 hands out, so lock the tree to SYSTEM + Administrators
# (users may read and run, which `gamemoded arch` needs).
$ErrorActionPreference = 'Continue'
icacls C:\gamemode /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' /T /C /Q
icacls C:\gamemode
# The OpenSSH admin key file must be SYSTEM + Administrators only, or sshd ignores it.
$k = 'C:\ProgramData\ssh\administrators_authorized_keys'
icacls $k /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F'
icacls $k
Get-Content $k | ForEach-Object { ($_ -split ' ')[0,2] -join ' ' }
