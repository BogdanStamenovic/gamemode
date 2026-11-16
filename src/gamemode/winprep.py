"""Prepare the Windows install offline, from Linux, through the mounted partition.

Everything here is idempotent: rerunning `gaming prepare` re-copies files and
re-checks registry values, changing only what differs.

Layout planted on C: (C:\\gamemode):
  bin\\gamemoded.exe      the SYSTEM service (Go, cross-compiled from windows/)
  tasks\\NN-name.ps1      run once each at the next boot, in name order
  reports\\               results: latest.json, boot-*.json, task-*.json/.log
  dl\\                    installers the tasks use (Apollo)
  secrets\\apollo.json    Apollo web-UI credentials (C:\\gamemode is locked to
                         SYSTEM + Administrators by task 00-acl)
  config.json            service config (relay URL + token, deadlines)
  mode.json              written by `gaming on`; absent = passive boot
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
import time
from collections.abc import Callable
from pathlib import Path

from . import hive
from .config import REPO, Config
from .hive import REG_DWORD, REG_EXPAND_SZ, REG_SZ, Value, dword, sz

SERVICE = "gamemoded"


class PrepError(Exception):
    pass


def hives(cfg: Config) -> dict[str, Path]:
    d = cfg.windows_mount / "Windows/System32/config"
    return {"SYSTEM": d / "SYSTEM", "SOFTWARE": d / "SOFTWARE"}


def preflight(cfg: Config) -> list[str]:
    """Problems that make an offline edit unsafe. Empty list = go."""
    problems = []
    m = cfg.windows_mount
    if not m.is_mount():
        problems.append(f"{m} is not mounted")
        return problems
    if (m / "hiberfil.sys").exists():
        problems.append("hiberfil.sys exists: Windows may be hibernated; editing it now loses data")
    for name, p in hives(cfg).items():
        if not p.exists():
            problems.append(f"{name} hive missing at {p}")
        elif not hive.is_clean(p):
            problems.append(f"{name} hive is dirty (unreplayed transaction log)")
    return problems


def backup(cfg: Config, log: Callable[[str], None]) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = cfg.backup_dir if cfg.backup_dir.is_dir() else Path.home() / ".local/state/gamemode"
    dest = base / f"gamemode-backup-{stamp}"
    dest.mkdir(parents=True)
    with tarfile.open(dest / "hives.tar", "w") as t:
        for name, p in hives(cfg).items():
            t.add(p, arcname=name)
    efi = Path("/boot/efi")
    if efi.is_dir():
        with tarfile.open(dest / "esp.tar", "w") as t:
            t.add(efi, arcname="efi")
    r = subprocess.run(["efibootmgr", "-v"], capture_output=True, text=True)
    (dest / "efibootmgr-v.txt").write_text(r.stdout + r.stderr)
    log(f"backup: {dest}")
    return dest


def registry_values(cfg: Config, ccs: str) -> dict[str, list[Value]]:
    svc = f"{ccs}\\Services\\{SERVICE}"
    fw = f"{ccs}\\Services\\SharedAccess\\Parameters\\FirewallPolicy\\FirewallRules"
    # Same string format Windows writes; no Profile= means every profile,
    # because the wired LAN may be classed Public and the stock sshd rule is Private-only.
    rule = ("v2.33|Action=Allow|Active=TRUE|Dir=In|Protocol=6|LPort=22|RA4=LocalSubnet|"
            "App=%SystemRoot%\\system32\\OpenSSH\\sshd.exe|Name=gamemode: sshd from LAN (offline)|"
            "Desc=Planted by gamemode so pigion can always reach Windows|")
    system = [
        Value(svc, "Type", REG_DWORD, dword(0x10)),
        Value(svc, "Start", REG_DWORD, dword(2)),
        Value(svc, "ErrorControl", REG_DWORD, dword(1)),
        Value(svc, "ImagePath", REG_EXPAND_SZ, sz(r'"C:\gamemode\bin\gamemoded.exe" service')),
        Value(svc, "ObjectName", REG_SZ, sz("LocalSystem")),
        Value(svc, "DisplayName", REG_SZ, sz("gamemode")),
        Value(svc, "Description", REG_SZ, sz(
            "Runs gamemode's task queue, restarts into Arch when idle or asked. "
            "github.com/BogdanStamenovic/gamemode")),
        # Restart the service after 30 s on its first two failures; the hard
        # deadline lives in pigion too, so a service that keeps dying still ends in Arch.
        Value(svc, "FailureActions", hive.REG_BINARY, (
            dword(86400) + dword(0) + dword(0) + dword(3) + dword(0x14)
            + dword(1) + dword(30000) + dword(1) + dword(30000) + dword(0) + dword(0))),
        Value(f"{ccs}\\Control\\Session Manager\\Power", "HiberbootEnabled", REG_DWORD, dword(0)),
        Value(f"{ccs}\\Control\\Power", "HibernateEnabled", REG_DWORD, dword(0)),
        Value(fw, "gamemode-sshd-offline", REG_SZ, sz(rule)),
        # Arch keeps the RTC in UTC; Windows by default writes local time to it,
        # so Arch booted an hour off after the first Windows stint (measured
        # 2026-11-16: journal started at 22:45 for 23:45 CET until NTP caught up).
        Value(f"{ccs}\\Control\\TimeZoneInformation", "RealTimeIsUniversal",
              hive.REG_QWORD, hive.qword(1)),
    ]
    # A Defender path exclusion for the service binary only: an unsigned Go
    # binary running as SYSTEM is exactly what heuristics like to quarantine,
    # and a quarantined service means Windows never restarts by itself.
    software = [
        Value("Policies\\Microsoft\\Windows Defender\\Exclusions", "Exclusions_Paths",
              REG_DWORD, dword(1)),
        Value("Policies\\Microsoft\\Windows Defender\\Exclusions\\Paths",
              "C:\\gamemode\\bin\\gamemoded.exe", REG_SZ, sz("0")),
    ]
    return {"SYSTEM": system, "SOFTWARE": software}


def _pubkeys(cfg: Config) -> list[str]:
    keys = []
    for spec in cfg.authorized_keys:
        if spec.startswith("ssh:"):
            r = subprocess.run(["ssh", "-o", "BatchMode=yes", spec[4:], "cat ~/.ssh/id_ed25519.pub"],
                               capture_output=True, text=True, timeout=20)
            if r.returncode != 0:
                raise PrepError(f"could not read {spec[4:]}'s public key: {r.stderr.strip()}")
            keys.append(r.stdout.strip())
        elif spec.startswith("ssh-"):
            keys.append(spec.strip())
        else:
            keys.append(Path(spec).expanduser().read_text().strip())
    return keys


def authorize_keys(cfg: Config, log: Callable[[str], None]) -> int:
    """Append missing keys. Appends in place so the file keeps its ACL."""
    p = cfg.windows_mount / "ProgramData/ssh/administrators_authorized_keys"
    existing = p.read_text(errors="replace") if p.exists() else ""
    have = {line.split()[1] for line in existing.splitlines() if len(line.split()) >= 2}
    added = 0
    with open(p, "a", newline="\r\n") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        for k in _pubkeys(cfg):
            if k.split()[1] not in have:
                f.write(k + "\n")
                log(f"  authorized: {' '.join(k.split()[::2])}")
                added += 1
    return added


def build_service(log: Callable[[str], None]) -> Path:
    out = REPO / "build/gamemoded.exe"
    src = REPO / "windows"
    newest = max(f.stat().st_mtime for f in src.rglob("*.go"))
    if out.exists() and out.stat().st_mtime >= newest:
        return out
    from . import __version__

    log("building gamemoded.exe (GOOS=windows)")
    r = subprocess.run(
        ["go", "build", "-trimpath", "-ldflags", f"-s -w -X main.version={__version__}",
         "-o", str(out), "./cmd/gamemoded"],
        cwd=src, capture_output=True, text=True,
        env={**__import__("os").environ, "GOOS": "windows", "GOARCH": "amd64", "CGO_ENABLED": "0"},
    )
    if r.returncode != 0:
        raise PrepError(f"go build failed:\n{r.stderr}")
    return out


def _copy_if_changed(src: Path, dst: Path, log: Callable[[str], None]) -> bool:
    if dst.exists() and dst.read_bytes() == src.read_bytes():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    log(f"  copied {dst}")
    return True


def plant_files(cfg: Config, log: Callable[[str], None], reset_tasks: list[str]) -> None:
    gm = cfg.gm_dir
    for sub in ("bin", "tasks", "reports", "dl", "secrets"):
        (gm / sub).mkdir(parents=True, exist_ok=True)
    _copy_if_changed(build_service(log), gm / "bin/gamemoded.exe", log)
    # Repo tasks plus local ones (~/.config/gamemode/tasks): one-off jobs that
    # belong to this machine, not to the tool (e.g. a cleaning run).
    from .config import config_dir

    wanted = sorted([*(REPO / "windows/tasks").glob("*.ps1"), *(config_dir() / "tasks").glob("*.ps1")],
                    key=lambda t: t.name)
    for t in wanted:
        if _copy_if_changed(t, gm / "tasks" / t.name, log):
            reset_tasks.append(t.stem)  # a changed task runs again
    for stale in (gm / "tasks").glob("*.ps1"):
        if stale.name not in {t.name for t in wanted}:
            stale.unlink()
            log(f"  removed stale task {stale.name}")
    for name in reset_tasks:
        for ext in (".json", ".log"):
            (gm / "reports" / f"task-{name}{ext}").unlink(missing_ok=True)
    for inst in sorted((REPO / ".cache").glob("Apollo-*.exe")):
        _copy_if_changed(inst, gm / "dl" / inst.name, log)
    (gm / "secrets/apollo.json").write_text(json.dumps(
        {"user": cfg.apollo_user, "password": cfg.apollo_password}))
    (gm / "config.json").write_text(json.dumps({
        "relay_mac": cfg.relay_mac,
        "relay_port": cfg.relay_port,
        "relay_fallbacks": cfg.relay_fallbacks,
        "relay_token": cfg.relay_token,
        "idle_minutes": cfg.idle_minutes,
        "prep_minutes": cfg.prep_minutes,
        "grub_guid": cfg.grub_guid,
    }, indent=2))


def prepare(cfg: Config, log: Callable[[str], None], dry_run: bool = False,
            rerun: list[str] | None = None) -> None:
    problems = preflight(cfg)
    if problems:
        raise PrepError("; ".join(problems))
    sysh = hives(cfg)["SYSTEM"]
    ccs = hive.current_control_set(sysh)
    vals = registry_values(cfg, ccs)
    pending = {
        name: [v for v in vs if hive.read_value(hives(cfg)[name], v.key, v.name) != (v.type, v.data)]
        for name, vs in vals.items()
    }
    for name, vs in pending.items():
        for v in vs:
            log(f"{'would set' if dry_run else 'set'} {name}\\{v.key} : {v.name} = {hive._show(v)}")
    if dry_run:
        log(f"dry run: {sum(map(len, pending.values()))} registry value(s) differ; nothing written")
        return
    if any(pending.values()):
        backup(cfg, log)
        for name, vs in vals.items():
            n = hive.apply(hives(cfg)[name], vs, log)
            log(f"{name}: {n} value(s) changed, verified, written in place")
    else:
        log("registry already prepared")
    authorize_keys(cfg, log)
    plant_files(cfg, log, list(rerun or []))
    log(f"planted {cfg.gm_dir}")


def unprepare(cfg: Config, log: Callable[[str], None]) -> None:
    """Undo prepare(): service key, firewall rule, Defender exclusion, C:\\gamemode,
    our keys in administrators_authorized_keys. Apollo and HiberbootEnabled=0 stay
    (Apollo is a normal program now; Fast Startup must stay off for /mnt/offload)."""
    problems = preflight(cfg)
    if problems:
        raise PrepError("; ".join(problems))
    h = hives(cfg)
    ccs = hive.current_control_set(h["SYSTEM"])
    backup(cfg, log)
    fw = f"{ccs}\\Services\\SharedAccess\\Parameters\\FirewallPolicy\\FirewallRules"
    hive.delete(h["SYSTEM"], [f"{ccs}\\Services\\{SERVICE}"], [(fw, "gamemode-sshd-offline")], log)
    hive.delete(h["SOFTWARE"], [], [("Policies\\Microsoft\\Windows Defender\\Exclusions\\Paths",
                                     "C:\\gamemode\\bin\\gamemoded.exe")], log)
    keys = {k.split()[1] for k in _pubkeys(cfg)}
    p = cfg.windows_mount / "ProgramData/ssh/administrators_authorized_keys"
    if p.exists():
        lines = p.read_text(errors="replace").splitlines()
        kept = [ln for ln in lines if len(ln.split()) < 2 or ln.split()[1] not in keys]
        with open(p, "r+", newline="\r\n") as f:
            f.write("".join(ln + "\n" for ln in kept))
            f.truncate()
        log(f"  removed {len(lines) - len(kept)} key(s) from administrators_authorized_keys")
    if cfg.gm_dir.exists():
        shutil.rmtree(cfg.gm_dir)
        log(f"  removed {cfg.gm_dir}")
