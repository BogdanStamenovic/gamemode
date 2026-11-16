# The Windows hotline adapter, user-facing half: "Back to Arch" on the public
# desktop and in the all-users Start menu, with Ctrl+Alt+End as its hotkey
# (a .lnk hotkey works from the desktop or Start menu without any tray app).
$ErrorActionPreference = 'Continue'
$sh = New-Object -ComObject WScript.Shell
$targets = @(
  "$env:PUBLIC\Desktop\Back to Arch.lnk",
  "$env:ProgramData\Microsoft\Windows\Start Menu\Programs\Back to Arch.lnk"
)
foreach ($t in $targets) {
  $l = $sh.CreateShortcut($t)
  $l.TargetPath = 'C:\gamemode\bin\gamemoded.exe'
  $l.Arguments = 'arch'
  $l.WorkingDirectory = 'C:\gamemode'
  $l.Description = 'Restart archserver into Arch (gamemode)'
  $l.IconLocation = '%SystemRoot%\System32\shell32.dll,27'
  if ($t -like '*Start Menu*') { $l.Hotkey = 'CTRL+ALT+END' }
  $l.Save()
  "created $t"
}
# `gamemoded` on PATH for any shell.
$p = [Environment]::GetEnvironmentVariable('Path', 'Machine')
if ($p -notlike '*C:\gamemode\bin*') { [Environment]::SetEnvironmentVariable('Path', "$p;C:\gamemode\bin", 'Machine'); 'added C:\gamemode\bin to PATH' }
