"""Configuration: ~/.config/gamemode/config.toml plus generated secrets."""

from __future__ import annotations

import json
import os
import secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("GAMEMODE_CONFIG_DIR", Path.home() / ".config/gamemode"))
STATE_DIR = Path(os.environ.get("GAMEMODE_STATE_DIR", Path.home() / ".local/state/gamemode"))
REPO = Path(__file__).resolve().parents[2]

DEFAULT_TOML = """\
# gamemode configuration. Edit freely; `gaming prepare` re-reads it.

# Where the Windows partition is mounted read-write on archserver.
windows_mount = "/mnt/windows"
# UEFI entry for Windows Boot Manager (efibootmgr). Used only as BootNext.
windows_boot_entry = "0000"
# Firmware GUID of the GRUB entry as Windows sees it (bcdedit /enum {fwbootmgr}).
grub_guid = "{dacb2679-a1a5-11f1-9015-806e6f6e6963}"
# No LAN addresses are configured: hosts are found by MAC (neighbour table,
# then a /24 sweep) because DHCP leases move.
# archserver's wired NIC; Windows uses the same NIC.
windows_mac = "a8:a1:59:fd:4d:13"
windows_user = "Korisnik"

# The always-on relay (pigion): found by MAC, then the fallbacks (Tailscale).
relay_mac = "e4:5f:01:f1:e1:f4"
relay_port = 8797
relay_fallbacks = ["http://100.114.148.69:8797"]
relay_host = "pigion"

# Gaming mode: restart into Arch after this long with no Moonlight stream.
idle_minutes = 20
# Prep mode: hard deadline for the task queue before restarting into Arch.
prep_minutes = 30

# Where hive backups go before every offline edit.
backup_dir = "/mnt/offload"

# Public keys allowed to SSH into Windows as an administrator.
authorized_keys = ["~/.ssh/id_ed25519.pub", "ssh:pigion"]
"""


@dataclass
class Config:
    windows_mount: Path = Path("/mnt/windows")
    windows_boot_entry: str = "0000"
    grub_guid: str = "{dacb2679-a1a5-11f1-9015-806e6f6e6963}"
    windows_mac: str = "a8:a1:59:fd:4d:13"
    windows_user: str = "Korisnik"
    relay_mac: str = "e4:5f:01:f1:e1:f4"
    relay_port: int = 8797
    relay_fallbacks: list[str] = field(default_factory=lambda: ["http://100.114.148.69:8797"])
    relay_host: str = "pigion"
    idle_minutes: int = 20
    prep_minutes: int = 30
    backup_dir: Path = Path("/mnt/offload")
    authorized_keys: list[str] = field(default_factory=lambda: ["~/.ssh/id_ed25519.pub", "ssh:pigion"])
    relay_token: str = ""
    apollo_user: str = "gamemode"
    apollo_password: str = ""

    @property
    def gm_dir(self) -> Path:
        return self.windows_mount / "gamemode"


def config_dir() -> Path:
    # Read at call time so tests (and wrappers) can point it elsewhere.
    return Path(os.environ.get("GAMEMODE_CONFIG_DIR", CONFIG_DIR))


def ensure_files() -> None:
    CONFIG_DIR = config_dir()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cfg = CONFIG_DIR / "config.toml"
    if not cfg.exists():
        cfg.write_text(DEFAULT_TOML)
    sec = CONFIG_DIR / "secrets.json"
    if not sec.exists():
        sec.touch(mode=0o600)
        sec.write_text(json.dumps({
            "relay_token": secrets.token_urlsafe(24),
            "apollo_user": "gamemode",
            "apollo_password": secrets.token_urlsafe(18),
        }, indent=2))
    os.chmod(sec, 0o600)


def load() -> Config:
    ensure_files()
    CONFIG_DIR = config_dir()
    raw = tomllib.loads((CONFIG_DIR / "config.toml").read_text())
    raw.update(json.loads((CONFIG_DIR / "secrets.json").read_text()))
    c = Config()
    for k, v in raw.items():
        if hasattr(c, k):
            cur = getattr(c, k)
            setattr(c, k, Path(v) if isinstance(cur, Path) else v)
    return c
