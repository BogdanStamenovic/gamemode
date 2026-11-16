"""Linux side of a switch: is it safe, put agents to sleep, BootNext, reboot.

BootOrder and the GRUB default are never touched. Windows is reached only
through a one-shot BootNext, so any failure on the way falls back to Arch.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import Config

PATH_EXTRA = [str(Path.home() / ".claude/bin"), str(Path.home() / ".local/bin")]


def _env() -> dict[str, str]:
    return {**os.environ, "PATH": ":".join(PATH_EXTRA + [os.environ.get("PATH", "")])}


def _run(cmd: list[str], timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_env())


@dataclass
class Blocker:
    what: str
    detail: str


def efi_state() -> dict[str, str]:
    r = _run(["efibootmgr"])
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        m = re.match(r"(BootCurrent|BootNext|BootOrder|Timeout): (.*)", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def agents() -> list[tuple[str, str]]:
    """(name, status) for every live hotline agent."""
    if not shutil.which("hotline", path=_env()["PATH"]):
        return []
    r = _run(["hotline", "--list"])
    rows = []
    for line in r.stdout.splitlines():
        m = re.match(r"(\S+) .*\)\s+(busy|idle|working|asleep|\S+)", line)
        if m:
            rows.append((m.group(1), m.group(2)))
    return rows


def blockers(self_name: str | None) -> list[Blocker]:
    found = []
    for name, status in agents():
        if status == "busy" and name != self_name:
            found.append(Blocker("agent busy", name))
    if shutil.which("nvidia-smi"):
        r = _run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                  "--format=csv,noheader"])
        for line in r.stdout.strip().splitlines():
            found.append(Blocker("GPU job", line.strip()))
    lock = Path("/run/user") / str(os.getuid()) / "docker-use"
    if lock.is_dir() and any(lock.iterdir()):
        found.append(Blocker("docker in use", ", ".join(p.name for p in lock.iterdir())))
    return found


def sleep_agents(self_name: str | None, log: Callable[[str], None]) -> None:
    """Put idle hotline agents to sleep so they resume cleanly after the switch.
    The operator is exempt from sleep and is respawned by hotline's watchdog."""
    names = [n for n, s in agents() if s == "idle" and n != self_name and not n.startswith("hotline-")]
    if not names:
        return
    # One call per agent: `hotline --sleep` takes a single NAME [never|now]
    # (passing five at once returned rc=2 and slept nobody, 2026-11-17).
    for n in names:
        r = _run(["hotline", "--sleep", n, "now"], timeout=60)
        log(f"hotline --sleep {n} now: rc={r.returncode} {r.stderr.strip()[:200]}")


def write_mode(cfg: Config, mode: str, deadline: int | None = None) -> None:
    m: dict[str, object] = {"mode": mode, "set_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    if deadline:
        m["deadline_minutes"] = deadline
    if mode == "gaming":
        m["idle_minutes"] = cfg.idle_minutes
    p = cfg.gm_dir / "mode.json"
    p.write_text(json.dumps(m))
    os.sync()


def set_bootnext(cfg: Config) -> None:
    r = _run(["sudo", "-n", "efibootmgr", "--bootnext", cfg.windows_boot_entry])
    if r.returncode != 0:
        raise RuntimeError(f"efibootmgr --bootnext failed: {r.stderr.strip()}")
    if efi_state().get("BootNext") != cfg.windows_boot_entry:
        raise RuntimeError("BootNext did not stick")


def clear_bootnext() -> None:
    _run(["sudo", "-n", "efibootmgr", "--delete-bootnext"])


def reboot() -> None:
    os.sync()
    r = _run(["sudo", "-n", "systemctl", "reboot"])
    if r.returncode != 0:
        raise RuntimeError(f"reboot failed: {r.stderr.strip()}")
