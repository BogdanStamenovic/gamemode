"""Can the GRUB EFI image on the ESP still find /boot/grub? And build one that always can.

Why this exists: on 2026-11-18 a Windows feature update (KB5132061) added a
recovery partition in front of Arch's root, so root moved from nvme0n1p4 to p5.
grubx64.efi (from a plain `grub-install`) had the prefix `(,gpt4)/boot/grub`
baked in and no UUID search, so GRUB dropped to a rescue prompt and the box was
dark for 42 minutes with nobody at the keyboard.

A GRUB core image is a PE file whose `mods` section holds a `struct
grub_module_info64` ("mimg", offset, size) followed by objects, each
`{u32 type, u32 size}` + payload: 0 = ELF module, 2 = embedded config,
3 = prefix (grub-core/kern/efi/init.c, include/grub/kernel.h).

The image built here carries an early config that finds the root filesystem by
UUID and sets the prefix from it, so partition renumbering cannot break it.
"""

from __future__ import annotations

import re
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ESP_IMAGE = Path("/boot/efi/EFI/GRUB/grubx64.efi")
MODDIR = Path("/boot/grub/x86_64-efi")  # what $prefix loads at boot (copied by grub-install)
PKGDIR = Path("/usr/lib/grub/x86_64-efi")  # the package; holds kernel.img, which MODDIR lacks
# What the early config needs before normal.mod is reachable: the partition
# table, root's filesystem, and the search command. Everything else loads from
# $prefix as usual.
EMBED_MODULES = ["part_gpt", "part_msdos", "ext2", "fat", "search_fs_uuid", "search", "echo"]

OBJ_ELF, OBJ_CONFIG, OBJ_PREFIX = 0, 2, 3
MIMG = 0x676D696D


class GrubImageError(Exception):
    pass


@dataclass
class GrubImage:
    prefix: str = ""
    config: str = ""
    modules: list[str] = field(default_factory=list)


@dataclass
class RootFs:
    uuid: str
    partnum: int  # GPT partition number of the filesystem holding the grub dir
    grub_path: str  # the grub directory as GRUB sees it on that filesystem, e.g. /boot/grub


def _modname(elf: bytes) -> str:
    try:
        shoff = struct.unpack_from("<Q", elf, 0x28)[0]
        shentsize, shnum, shstrndx = struct.unpack_from("<HHH", elf, 0x3A)
        def sec(i: int) -> tuple[int, int, int]:
            o = shoff + i * shentsize
            name, = struct.unpack_from("<I", elf, o)
            off, size = struct.unpack_from("<QQ", elf, o + 0x18)
            return name, off, size
        _, stroff, _ = sec(shstrndx)
        for i in range(shnum):
            name, off, size = sec(i)
            if elf[stroff + name:].split(b"\0", 1)[0] == b".modname":
                return elf[off:off + size].split(b"\0", 1)[0].decode()
    except struct.error:
        pass
    return "?"


def parse(data: bytes) -> GrubImage:
    if data[:2] != b"MZ":
        raise GrubImageError("not a PE image")
    pe, = struct.unpack_from("<I", data, 0x3C)
    if data[pe:pe + 4] != b"PE\0\0":
        raise GrubImageError("no PE header")
    nsec, = struct.unpack_from("<H", data, pe + 6)
    optsize, = struct.unpack_from("<H", data, pe + 20)
    table = pe + 24 + optsize
    for k in range(nsec):
        name, _vsize, _va, rsize, rptr = struct.unpack_from("<8sIIII", data, table + 40 * k)
        if name.rstrip(b"\0") in (b"mods", b".mods"):
            break
    else:
        raise GrubImageError("no mods section: not a GRUB core image")
    magic, _pad, off, size = struct.unpack_from("<IIQQ", data, rptr)
    if magic != MIMG or size > rsize:
        raise GrubImageError("mods section has no module info header")
    img = GrubImage()
    p, end = rptr + off, rptr + size
    while p + 8 <= end:
        typ, osz = struct.unpack_from("<II", data, p)
        if osz < 8:
            break
        body = data[p + 8:p + osz]
        if typ == OBJ_ELF:
            img.modules.append(_modname(body))
        elif typ == OBJ_CONFIG:
            img.config += body.split(b"\0", 1)[0].decode(errors="replace")
        elif typ == OBJ_PREFIX:
            img.prefix = body.split(b"\0", 1)[0].decode(errors="replace")
        p += osz
    return img


def root_fs(grub_dir: Path = Path("/boot/grub")) -> RootFs:
    r = subprocess.run(["findmnt", "-n", "-o", "SOURCE,UUID,TARGET", "-T", str(grub_dir)],
                       capture_output=True, text=True, check=True)
    source, uuid, target = r.stdout.split()
    part = Path("/sys/class/block") / Path(source).name / "partition"
    partnum = int(part.read_text()) if part.exists() else 0
    rel = "/" + str(grub_dir.resolve().relative_to(target)).lstrip("/")
    return RootFs(uuid=uuid.lower(), partnum=partnum, grub_path=rel.rstrip("/") if rel != "/." else "/")


_SEARCH = re.compile(
    r"^\s*search(?:\.fs_uuid\s+(?P<u1>\S+)\s+(?P<v1>\w+)"
    r"|\s+(?=.*(?:--fs-uuid|-u)\b)(?=.*--set=(?P<v2>\w+)).*?(?P<u2>[0-9a-fA-F-]{9,}))", re.M)
_PREFIX = re.compile(r"^\s*set\s+prefix=[\"']?\(\$\{?(?P<var>\w+)\}?\)(?P<path>[^\s\"']*)", re.M)
_DEV_PREFIX = re.compile(r"^\((?:hd\d+)?,(?:gpt|msdos)(?P<n>\d+)\)(?P<path>/.*)$")


def check(img: GrubImage, root: RootFs) -> tuple[bool, str]:
    """(ok, why). ok = this image will reach root's grub directory on the current disk layout."""
    want = root.grub_path.rstrip("/")
    for m in _SEARCH.finditer(img.config):
        uuid = (m.group("u1") or m.group("u2") or "").lower()
        var = m.group("v1") or m.group("v2")
        pm = next((p for p in _PREFIX.finditer(img.config) if p.group("var") == var), None)
        if not pm:
            continue
        path = pm.group("path").rstrip("/")
        if uuid != root.uuid:
            return False, f"early config searches fs UUID {uuid}, root is {root.uuid}"
        if path != want:
            return False, f"early config sets prefix path {path}, grub is at {want}"
        missing = [x for x in ("search_fs_uuid", "part_gpt", "ext2") if x not in img.modules]
        if missing and "search" not in img.modules:
            return False, f"early config needs modules missing from the image: {missing}"
        return True, f"finds root by UUID {uuid} (survives partition renumbering)"
    m = _DEV_PREFIX.match(img.prefix)
    if m:
        n = int(m.group("n"))
        if m.group("path").rstrip("/") != want:
            return False, f"prefix {img.prefix}: grub is at {want}"
        if n != root.partnum:
            return False, (f"prefix {img.prefix} points at partition {n}, but root is partition "
                           f"{root.partnum}: GRUB would stop at a rescue prompt")
        return True, (f"prefix {img.prefix} matches root partition {n} today, but breaks if the "
                      "partitions are renumbered again (run `gaming grub install`)")
    return False, f"cannot tell how this image finds /boot/grub (prefix {img.prefix!r}, no UUID search)"


def check_esp(path: Path = ESP_IMAGE) -> tuple[bool, str]:
    try:
        img = parse(path.read_bytes())
        return check(img, root_fs())
    except (OSError, GrubImageError, subprocess.CalledProcessError, ValueError) as exc:
        return False, f"cannot check {path}: {exc}"


def early_config(root: RootFs) -> str:
    return (f"search.fs_uuid {root.uuid} root\n"
            f"set prefix=($root){root.grub_path}\n")


def module_skew(moddir: Path = MODDIR, pkgdir: Path = PKGDIR) -> list[str]:
    """Modules in the boot grub dir that differ from the package. The core image
    comes from the package (kernel.img only exists there) and loads the rest from
    $prefix at boot, so the two must be the same build."""
    return sorted(m.name for m in moddir.glob("*.mod")
                  if not (pkgdir / m.name).exists() or (pkgdir / m.name).read_bytes() != m.read_bytes())


def build(out: Path, root: RootFs, pkgdir: Path = PKGDIR, moddir: Path = MODDIR) -> GrubImage:
    skew = module_skew(moddir, pkgdir)
    if skew:
        raise GrubImageError(f"{moddir} is not the installed grub package's build ({len(skew)} modules "
                             f"differ, e.g. {skew[:3]}); run `sudo grub-install --no-nvram "
                             "--efi-directory=/boot/efi --bootloader-id=GRUB` first, then this again")
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "early.cfg"
        cfg.write_text(early_config(root))
        r = subprocess.run(["grub-mkimage", "-O", "x86_64-efi", "-d", str(pkgdir), "-c", str(cfg),
                            "-p", root.grub_path, "-o", str(out), *EMBED_MODULES],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise GrubImageError(f"grub-mkimage failed: {r.stderr.strip()}")
    img = parse(out.read_bytes())
    ok, why = check(img, root)
    if not ok:
        raise GrubImageError(f"freshly built image fails its own check: {why}")
    return img


def install(log, dry_run: bool = False) -> int:
    """Build, verify, keep the old image beside it, copy into place. Never touches NVRAM."""
    root = root_fs()
    ok, why = check_esp()
    log(f"current {ESP_IMAGE}: {'ok' if ok else 'BROKEN'}: {why}")
    if ok and "UUID" in why:
        log("already finds root by UUID; nothing to do")
        return 0
    with tempfile.TemporaryDirectory() as d:
        new = Path(d) / "grubx64.efi"
        build(new, root)
        log(f"built image with early config: {early_config(root).strip()!r}")
        if dry_run:
            log("dry run: not installing")
            return 0
        keep = ESP_IMAGE.with_name(f"grubx64.efi.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        for cmd in (["cp", "-p", str(ESP_IMAGE), str(keep)], ["cp", str(new), str(ESP_IMAGE)], ["sync"]):
            subprocess.run(["sudo", "-n", *cmd], check=True)
        log(f"kept the old image as {keep}")
    ok, why = check_esp()
    log(f"installed {ESP_IMAGE}: {'ok' if ok else 'BROKEN'}: {why}")
    return 0 if ok else 1



def main() -> int:
    """`python3 -m gamemode.grubimg`: what the pacman hook runs (as root, no config needed)."""
    ok, why = check_esp()
    skew = module_skew() if MODDIR.is_dir() and PKGDIR.is_dir() else []
    print(f"gamemode: {ESP_IMAGE}: {'ok' if ok else 'WILL NOT BOOT ARCH'}: {why}")
    if skew:
        print(f"gamemode: {MODDIR} is older than the grub package ({len(skew)} modules differ). "
              "That is normal until someone runs grub-install. If you do, run it with --no-nvram and "
              "then `gaming grub install`, because a plain grub-install puts a partition-number prefix back.")
    elif ok and "UUID" not in why:
        print("gamemode: run `gaming grub install` so GRUB finds root by UUID again.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
