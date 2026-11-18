"""GRUB image checks. Images are built with the real grub-mkimage, so these test
the parser against what GRUB actually writes, not against a hand-made fixture."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from gamemode import grubimg

PKG = grubimg.PKGDIR
needs_grub = pytest.mark.skipif(not (shutil.which("grub-mkimage") and (PKG / "kernel.img").exists()),
                                reason="grub-mkimage / x86_64-efi modules not installed")

UUID = "088c2ab7-625d-457f-a1c9-2f879fc42c54"
ROOT_P5 = grubimg.RootFs(uuid=UUID, partnum=5, grub_path="/boot/grub")


def mkimage(tmp: Path, prefix: str, config: str | None = None) -> grubimg.GrubImage:
    out = tmp / "core.efi"
    cmd = ["grub-mkimage", "-O", "x86_64-efi", "-d", str(PKG), "-p", prefix, "-o", str(out)]
    if config is not None:
        (tmp / "early.cfg").write_text(config)
        cmd += ["-c", str(tmp / "early.cfg")]
    subprocess.run(cmd + ["part_gpt", "ext2", "search_fs_uuid"], check=True, capture_output=True)
    return grubimg.parse(out.read_bytes())


@needs_grub
def test_partition_number_prefix_wrong_partition_fails(tmp_path: Path) -> None:
    # The 2026-11-18 incident: image says gpt4, root moved to partition 5.
    img = mkimage(tmp_path, "(,gpt4)/boot/grub")
    assert img.prefix == "(,gpt4)/boot/grub" and "part_gpt" in img.modules
    ok, why = grubimg.check(img, ROOT_P5)
    assert not ok and "partition 4" in why


@needs_grub
def test_partition_number_prefix_right_partition_passes_with_warning(tmp_path: Path) -> None:
    ok, why = grubimg.check(mkimage(tmp_path, "(,gpt5)/boot/grub"), ROOT_P5)
    assert ok and "renumbered" in why


@needs_grub
def test_uuid_early_config_passes(tmp_path: Path) -> None:
    img = mkimage(tmp_path, "/boot/grub", grubimg.early_config(ROOT_P5))
    ok, why = grubimg.check(img, ROOT_P5)
    assert ok and "UUID" in why
    # Still fine after a renumbering: the partition number plays no part.
    assert grubimg.check(img, grubimg.RootFs(UUID, 9, "/boot/grub"))[0]


@needs_grub
def test_uuid_of_another_filesystem_fails(tmp_path: Path) -> None:
    other = grubimg.RootFs(uuid="11111111-2222-3333-4444-555555555555", partnum=5, grub_path="/boot/grub")
    img = mkimage(tmp_path, "/boot/grub", grubimg.early_config(other))
    ok, why = grubimg.check(img, ROOT_P5)
    assert not ok and "11111111" in why


@needs_grub
def test_bare_prefix_without_search_fails(tmp_path: Path) -> None:
    # A bare path resolves against the ESP GRUB was loaded from, which has no /boot/grub.
    assert not grubimg.check(mkimage(tmp_path, "/boot/grub"), ROOT_P5)[0]


@needs_grub
def test_build_produces_checked_image(tmp_path: Path) -> None:
    if grubimg.module_skew():
        pytest.skip("installed grub dir differs from the package")
    img = grubimg.build(tmp_path / "x.efi", ROOT_P5)
    assert "search_fs_uuid" in img.modules and UUID in img.config


def test_not_a_grub_image() -> None:
    with pytest.raises(grubimg.GrubImageError):
        grubimg.parse(b"MZ" + b"\0" * 200)
