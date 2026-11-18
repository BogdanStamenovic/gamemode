#!/bin/sh
# Install (or with --remove, uninstall) the system boot hook. System scope
# because it must run at boot with nobody logged in and after /mnt/windows mounts.
set -eu
tool=$(cd "$(dirname "$0")/.." && pwd)
unit=/etc/systemd/system/gamemode-boot.service
hook=/etc/pacman.d/hooks/gamemode-grub-check.hook
if [ "${1:-}" = --remove ]; then
  sudo systemctl disable gamemode-boot.service 2>/dev/null || true
  sudo rm -f "$unit" "$hook"
  sudo systemctl daemon-reload
  echo "removed $unit and $hook" >&2
  exit 0
fi
sed -e "s#@TOOL@#$tool#g" -e "s#@USER@#$(id -un)#g" -e "s#@WINDOWS_MOUNT@#/mnt/windows#g" \
  "$tool/systemd/gamemode-boot.service.in" | sudo tee "$unit.tmp" >/dev/null
sudo mv "$unit.tmp" "$unit"
sudo systemctl daemon-reload
sudo systemctl enable gamemode-boot.service >/dev/null 2>&1
echo "installed $unit (runs at every boot)" >&2
sudo mkdir -p "$(dirname "$hook")"
sed -e "s#@TOOL@#$tool#g" "$tool/pacman/gamemode-grub-check.hook.in" | sudo tee "$hook.tmp" >/dev/null
sudo mv "$hook.tmp" "$hook"
echo "installed $hook (checks GRUB after every grub upgrade)" >&2
