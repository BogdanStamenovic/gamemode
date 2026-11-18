#!/usr/bin/env bash
# Boot GRUB EFI images in QEMU+OVMF against synthetic copies of this disk, with
# root at different GPT partition numbers, and report which ones reach /boot/grub.
#
#   scripts/grub-qemu-test.sh WORKDIR IMAGE.efi [IMAGE.efi ...]
#
# Each test disk: p1 = a copy of the real ESP with the candidate as
# EFI/BOOT/BOOTX64.EFI (OVMF's removable-media path, so no NVRAM entry is
# needed), small filler partitions, and an ext4 copy of /boot that carries
# root's real filesystem UUID, at partition 4 ("old" layout, before the
# 2026-11-18 Windows update) and at partition 5 ("new" layout, today).
# The ext4 copy is never mounted or attached to a loop device on the host,
# so nothing on the host ever sees a second filesystem with root's UUID.
#
# Verdict comes from a screenshot ~8 s after power-on, OCR-free: GRUB's
# graphical menu vs. the text "grub rescue>" prompt is decided by the caller
# reading WORKDIR/*.png (printed at the end). Needs sudo (reads the ESP and /boot).
set -euo pipefail
work=$1; shift
mkdir -p "$work"
uuid=$(findmnt -n -o UUID -T /boot/grub)
ovmf=/usr/share/edk2/x64

boot_tar=$work/boot.tar
[ -s "$work/root.ext4" ] || {
  sudo -n tar -C / --exclude=boot/efi -cf "$boot_tar" boot
  sudo -n chown "$(id -u)" "$boot_tar"
  rm -rf "$work/rootfs"; mkdir -p "$work/rootfs"
  tar -C "$work/rootfs" -xf "$boot_tar"
  truncate -s 400M "$work/root.ext4"
  mkfs.ext4 -q -F -U "$uuid" -L gmtest -d "$work/rootfs" "$work/root.ext4"
  rm -rf "$work/rootfs" "$boot_tar"
}
[ -s "$work/esp.base" ] || { sudo -n dd if="$(findmnt -n -o SOURCE /boot/efi)" of="$work/esp.base" bs=1M status=none; sudo -n chown "$(id -u)" "$work/esp.base"; }

make_disk() {  # make_disk OUT ESP_IMG ROOTPART(4|5)
  local out=$1 esp=$2 rp=$3
  rm -f "$out"; truncate -s 600M "$out"
  {
    echo "label: gpt"
    echo "start=2048, size=100MiB, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
    echo "size=16MiB, type=E3C9E316-0B5C-4DB8-817D-F92DF00215AE"
    echo "size=8MiB, type=EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"
    [ "$rp" = 5 ] && echo "size=8MiB, type=DE94BBA4-06D1-4D40-A16A-BFD50179D6AC"
    echo "size=400MiB, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4"
  } | sfdisk -q "$out"
  local s
  s=$(sfdisk -J "$out" | python3 -c 'import json,sys; p=json.load(sys.stdin)["partitiontable"]["partitions"]; print(p[0]["start"], p[-1]["start"])')
  set -- $s
  dd if="$esp" of="$out" bs=512 seek="$1" conv=notrunc status=none
  dd if="$work/root.ext4" of="$out" bs=512 seek="$2" conv=notrunc status=none
}

for img in "$@"; do
  name=$(basename "$img" .efi)
  esp=$work/esp-$name.img
  cp "$work/esp.base" "$esp"
  mnt=$work/mnt; mkdir -p "$mnt"
  sudo -n mount -o loop "$esp" "$mnt"
  sudo -n mkdir -p "$mnt/EFI/BOOT"
  sudo -n rm -f "$mnt/EFI/Boot/bootx64.efi" "$mnt/EFI/BOOT/BOOTX64.EFI"
  sudo -n cp "$img" "$mnt/EFI/BOOT/BOOTX64.EFI"
  sudo -n umount "$mnt"
  for rp in 4 5; do
    disk=$work/disk-$name-p$rp.img
    make_disk "$disk" "$esp" "$rp"
    cp "$ovmf/OVMF_VARS.4m.fd" "$work/vars.fd"
    mon=$work/mon.sock; rm -f "$mon"
    qemu-system-x86_64 -machine q35,accel=kvm -m 1024 -display none -no-reboot \
      -drive if=pflash,format=raw,readonly=on,file=$ovmf/OVMF_CODE.4m.fd \
      -drive if=pflash,format=raw,file="$work/vars.fd" \
      -drive file="$disk",format=raw,if=virtio \
      -net none -monitor unix:"$mon",server,nowait &
    q=$!
    sleep 9
    printf 'screendump %s -f png\n' "$work/$name-p$rp.png" | python3 -c '
import socket,sys,time
s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); time.sleep(.3)
s.sendall(sys.stdin.read().encode()); time.sleep(1.5); s.sendall(b"quit\n"); time.sleep(.5)' "$mon"
    wait "$q" || true
    rm -f "$disk"
    echo "$name root=p$rp -> $work/$name-p$rp.png"
  done
  rm -f "$esp"
done
