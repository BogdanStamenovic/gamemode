"""Command-line interface for gamemode (`gaming`).

Keeps stdout clean for real output (status JSON, reports); all progress,
warnings and errors go to stderr.
Exit codes: 0 success, 1 one or more operations failed, 2 usage error /
refused (busy machine, aborted).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from . import __version__, boot, config, relay, winprep
from .hive import HiveError


class _UsageError(Exception):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def _build_parser() -> argparse.ArgumentParser:
    p = _ArgumentParser(prog="gaming", description="Switch archserver into Windows for game "
                        "streaming and back, headlessly.")
    p.add_argument("-v", "--verbose", action="store_true", help="print detailed progress")
    p.add_argument("-q", "--quiet", action="store_true", help="suppress non-error output")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    on = sub.add_parser("on", help="reboot into Windows and stream")
    on.add_argument("--prep", action="store_true",
                    help="prep boot: run the task queue, then come straight back")
    on.add_argument("--deadline", type=int, help="prep: minutes before a forced return")
    on.add_argument("--force", action="store_true", help="switch even if something is busy")
    on.add_argument("--no-reboot", action="store_true",
                    help="arm everything and set BootNext, but leave the reboot to the caller")
    on.add_argument("--dry-run", action="store_true", help="show what would happen")

    sub.add_parser("off", help="ask Windows (through the relay) to restart into Arch")
    sub.add_parser("status", help="relay, firmware and last-report state as JSON")

    rep = sub.add_parser("report", help="print the Windows-side reports")
    rep.add_argument("task", nargs="?", help="print one task's full output")

    prep = sub.add_parser("prepare", help="plant the service, tasks and registry edits offline")
    prep.add_argument("--dry-run", action="store_true", help="show registry changes only")
    prep.add_argument("--rerun", action="append", default=[], metavar="TASK",
                      help="forget a task's result so it runs again (repeatable)")

    un = sub.add_parser("unprepare", help="remove the service and C:\\gamemode from Windows "
                        "(Apollo stays installed)")
    un.add_argument("--yes", action="store_true", help="do not ask")
    pr = sub.add_parser("pair", help="(laptop) pair Moonlight with Apollo, no clicking")
    pr.add_argument("--wait", type=int, default=0, help="seconds to wait for archserver to appear")
    be = sub.add_parser("bench", help="(laptop) stream for a while and measure latency and bitrate")
    be.add_argument("--seconds", type=int, default=60)
    be.add_argument("--app", default="Desktop")
    be.add_argument("--bitrate", type=int, default=40000, help="kbit/s requested")
    be.add_argument("--resolution", default="1920x1080")
    be.add_argument("--fps", type=int, default=60)
    be.add_argument("--wait", type=int, default=0, help="seconds to wait for archserver to appear")
    sub.add_parser("boot-hook", help="run at Linux boot: disarm relay, archive reports")
    rd = sub.add_parser("relay-deploy", help="install or update the relay on pigion")
    rd.add_argument("--remove", action="store_true", help="uninstall it instead")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except _UsageError as exc:
        print(f"gaming: error: {exc}", file=sys.stderr)
        return 2

    def log(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr, flush=True)

    cfg = config.load()
    try:
        return int(COMMANDS[args.cmd](cfg, args, log))
    except (winprep.PrepError, HiveError, relay.RelayError, RuntimeError) as exc:
        print(f"gaming: error: {exc}", file=sys.stderr)
        return 1


def _self_agent() -> str | None:
    """This session's hotline name, so `gaming on` run by an agent does not
    count the agent itself as a blocker."""
    return os.environ.get("HOTLINE_AGENT") or None


def cmd_on(cfg: config.Config, a: argparse.Namespace, log) -> int:
    mode = "prep" if a.prep else "gaming"
    if not (cfg.gm_dir / "bin/gamemoded.exe").exists():
        log("Windows is not prepared; run `gaming prepare` first")
        return 1
    problems = winprep.preflight(cfg)
    if problems:
        log("refusing: " + "; ".join(problems))
        return 1
    blockers = boot.blockers(_self_agent())
    for b in blockers:
        log(f"busy: {b.what}: {b.detail}")
    if blockers and not a.force:
        log("refusing to switch while something is busy (--force overrides)")
        return 2
    efi = boot.efi_state()
    if efi.get("BootOrder", "").split(",")[0] == cfg.windows_boot_entry:
        log(f"refusing: BootOrder is {efi.get('BootOrder')}, Windows is first, so a failure "
            "would not fall back to Arch")
        return 1
    if a.dry_run:
        log(f"dry run: would write mode={mode}, arm relay, sleep idle agents, "
            f"BootNext={cfg.windows_boot_entry}, reboot={not a.no_reboot}")
        return 0
    boot.write_mode(cfg, mode, a.deadline)
    log(f"mode.json: {mode}")
    try:
        relay.call(cfg, "POST", "/arm", {
            "mode": mode, "windows_mac": cfg.windows_mac, "ssh_user": cfg.windows_user,
            # pigion's outer bound: prep deadline + boot slack; gaming has none (idle rules).
            "deadline_minutes": (a.deadline or cfg.prep_minutes) + 15 if mode == "prep" else None,
        })
        log("relay armed")
    except relay.RelayError as exc:
        log(f"warning: relay not armed ({exc}); Windows still has its own deadline")
    boot.set_bootnext(cfg)
    log(f"BootNext={cfg.windows_boot_entry} (one shot; BootOrder untouched)")
    if a.no_reboot:
        log("not rebooting (--no-reboot); the next reboot goes to Windows once")
        return 0
    boot.sleep_agents(_self_agent(), log)
    log("rebooting into Windows")
    boot.reboot()
    return 0


def cmd_off(cfg: config.Config, a: argparse.Namespace, log) -> int:
    if os.uname().nodename == "archserver":
        log("already in Arch")
        return 0
    relay.call(cfg, "POST", "/off", {"by": f"gaming off on {os.uname().nodename}"})
    log("relay told Windows to restart into Arch (picked up within ~15 s)")
    return 0


def _report_dir(cfg: config.Config) -> Path:
    return cfg.gm_dir / "reports"


def cmd_status(cfg: config.Config, a: argparse.Namespace, log) -> int:
    out: dict[str, object] = {"efi": boot.efi_state()}
    try:
        out["relay"] = relay.call(cfg, "GET", "/status")
    except relay.RelayError as exc:
        out["relay"] = {"error": str(exc)}
    latest = _report_dir(cfg) / "latest.json"
    if latest.exists():
        r = json.loads(latest.read_text())
        out["windows_last_boot"] = {k: r.get(k) for k in (
            "version", "started", "mode", "phase", "tasks", "restart_reason", "relay_ok")}
    out["mode_file"] = (cfg.gm_dir / "mode.json").exists()
    print(json.dumps(out, indent=2))
    return 0


def cmd_report(cfg: config.Config, a: argparse.Namespace, log) -> int:
    d = _report_dir(cfg)
    if a.task:
        p = d / f"task-{a.task}.log"
        if not p.exists():
            log(f"no log for task {a.task}")
            return 1
        sys.stdout.write(p.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n"))
        return 0
    latest = d / "latest.json"
    if not latest.exists():
        log("no Windows report yet")
        return 1
    r = json.loads(latest.read_text())
    print(f"boot {r.get('started')}  mode={r.get('mode', {}).get('mode')}  phase={r.get('phase')}  "
          f"restart: {r.get('restart_reason')}")
    for t in sorted(d.glob("task-*.json")):
        tr = json.loads(t.read_text())
        flag = "TIMEOUT" if tr.get("timed_out") else f"exit {tr.get('exit_code')}"
        print(f"  {tr['name']:<16} {flag:<8} {tr.get('seconds', 0):6.0f}s  {tr.get('finished')}")
    samples = [x for x in r.get("samples") or [] if x.get("streaming")]
    if samples:
        mb = sorted(x["mbit"] for x in samples)
        print(f"  streaming: {len(samples) * 15 // 60} min sampled, upload avg {sum(mb) / len(mb):.1f} / "
              f"median {mb[len(mb) // 2]:.1f} / max {mb[-1]:.1f} Mbit/s")
    for e in r.get("events", [])[-15:]:
        print(f"  | {e}")
    return 0


def cmd_prepare(cfg: config.Config, a: argparse.Namespace, log) -> int:
    winprep.prepare(cfg, log, dry_run=a.dry_run, rerun=a.rerun)
    return 0


def cmd_unprepare(cfg: config.Config, a: argparse.Namespace, log) -> int:
    if not a.yes:
        if not sys.stdin.isatty():
            log("refusing without --yes (stdin is not a TTY)")
            return 2
        log("Remove the gamemode service, its registry entries and C:\\gamemode? [y/N] ")
        if input().strip().lower() not in ("y", "yes"):
            return 2
    winprep.unprepare(cfg, log)
    return 0


def cmd_pair(cfg: config.Config, a: argparse.Namespace, log) -> int:
    from . import client

    try:
        client.pair(cfg, client.find_server(cfg, a.wait, log), log)
    except client.ClientError as exc:
        log(f"gaming: {exc}")
        return 1
    return 0


def cmd_bench(cfg: config.Config, a: argparse.Namespace, log) -> int:
    from . import client

    try:
        client.find_server(cfg, a.wait, log)
        r = client.bench(cfg, a.seconds, a.app, a.bitrate, a.resolution, a.fps, log)
    except client.ClientError as exc:
        log(f"gaming: {exc}")
        return 1
    print(json.dumps(r, indent=2))
    return 0


def cmd_boot_hook(cfg: config.Config, a: argparse.Namespace, log) -> int:
    """Back in Arch: disarm the relay and keep a copy of what Windows reported."""
    boot.clear_bootnext()
    # Retry: on 2026-11-17 the unit ran at boot before the LAN answered (even after
    # network-online.target) and left the relay armed. 18 x 10 s covers a slow DHCP.
    session = None
    for attempt in range(18):
        try:
            session = relay.call(cfg, "POST", "/disarm", {}).get("session")
            break
        except relay.RelayError as exc:
            log(f"relay (attempt {attempt + 1}): {exc}")
            time.sleep(10)
    d = _report_dir(cfg)
    marker = config.STATE_DIR / "last-archived"
    started = ""
    if (d / "latest.json").exists():
        started = str(json.loads((d / "latest.json").read_text()).get("started", ""))
    if marker.exists() and marker.read_text() == started:
        log("no new Windows boot since the last archive")
        return 0
    if d.is_dir() and started:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        marker.write_text(started)
        dest = config.STATE_DIR / "sessions" / time.strftime("%Y%m%d-%H%M%S")
        dest.mkdir(parents=True, exist_ok=True)
        for f in d.iterdir():
            if f.is_file():
                (dest / f.name).write_bytes(f.read_bytes())
        if session:
            (dest / "relay-session.json").write_text(json.dumps(session, indent=2))
        log(f"archived Windows reports to {dest}")
    return 0


RELAY_UNIT = """[Unit]
Description=gamemode relay (heartbeats, watchdog and Discord 'gaming off' while archserver runs Windows)
After=network-online.target

[Service]
Type=simple
EnvironmentFile=%h/.config/gamemode-relay.env
ExecStart=/usr/bin/python3 %h/.local/share/gamemode-relay/gamemode_relay.py
Restart=always
RestartSec=10
# A Pi Zero 2 W with ~180 MB free; the relay idles around 15 MB.
MemoryMax=48M

[Install]
WantedBy=default.target
"""


def cmd_relay_deploy(cfg: config.Config, a: argparse.Namespace, log) -> int:
    host = cfg.relay_host
    q = shlex.quote
    if a.remove:
        script = ("systemctl --user disable --now gamemode-relay.service 2>/dev/null; "
                  "rm -f ~/.config/systemd/user/gamemode-relay.service; "
                  "rm -rf ~/.local/share/gamemode-relay; systemctl --user daemon-reload; "
                  "echo 'relay removed (kept ~/.config/gamemode-relay.env and its state)'")
        return subprocess.run(["ssh", host, script]).returncode
    src = (config.REPO / "relay/gamemode_relay.py").read_text()
    # The relay borrows the Discord bot hotline's front door already holds on
    # pigion, so no second bot token has to exist anywhere.
    env = (f"GAMEMODE_TOKEN={cfg.relay_token}\nGAMEMODE_LISTEN=0.0.0.0:8797\n")
    script = f"""set -e
mkdir -p ~/.local/share/gamemode-relay ~/.config/systemd/user
cat > ~/.local/share/gamemode-relay/gamemode_relay.py <<'GM_EOF'
{src}GM_EOF
cat > ~/.config/systemd/user/gamemode-relay.service <<'GM_EOF'
{RELAY_UNIT}GM_EOF
umask 077
{{ printf %s {q(env)}
  grep -E '^(SENTINEL_BOT_TOKEN|DISCORD_USER_ID)=' ~/.config/hotline-frontdoor.env || true
}} > ~/.config/gamemode-relay.env
systemctl --user daemon-reload
systemctl --user enable gamemode-relay.service >/dev/null 2>&1
systemctl --user restart gamemode-relay.service
sleep 2
systemctl --user is-active gamemode-relay.service
"""
    r = subprocess.run(["ssh", host, "bash -s"], input=script, text=True, capture_output=True)
    log(r.stdout.strip() + r.stderr.strip())
    if r.returncode != 0:
        return 1
    st = relay.call(cfg, "GET", "/status")
    log(f"relay on {host} answers: active={st.get('active')}")
    return 0


COMMANDS = {
    "on": cmd_on, "off": cmd_off, "status": cmd_status, "report": cmd_report,
    "prepare": cmd_prepare, "unprepare": cmd_unprepare, "boot-hook": cmd_boot_hook,
    "pair": cmd_pair, "bench": cmd_bench, "relay-deploy": cmd_relay_deploy,
}
