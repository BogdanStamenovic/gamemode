#!/usr/bin/env python3
"""gamemode relay: the always-on half that stays up while archserver is in Windows.

Runs on pigion (Pi Zero 2 W, ~180 MB free), so it is the standard library only
and does nothing until a session is armed.

What it does during a session:
  * takes heartbeats from the Windows service and hands back a pending command
    ("off" means restart into Arch);
  * watches that the heartbeats keep coming, and if they do not, SSHes into
    Windows and restarts it -- the external leg of the hard timeout, for when
    the Windows service itself is dead or quarantined;
  * polls his Discord DMs with the bot for "gaming off" while Linux is down;
  * says so in that DM when the session starts, ends, or needs a hand.

HTTP API (bearer token on everything but GET /status):
  POST /arm        {mode, windows_mac, ssh_user, deadline_minutes}   from archserver
  POST /heartbeat  {...service snapshot...} -> {"command": "off"|null}  from Windows
  POST /off        {}                                               from anyone with the token
  POST /disarm     {}                                               from archserver, back in Arch
  GET  /status     public, no secrets; hotline's sentinel can read it
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATE_DIR = Path(os.environ.get("GAMEMODE_STATE", Path.home() / ".local/state/gamemode-relay"))
STATE_FILE = STATE_DIR / "state.json"
LISTEN = os.environ.get("GAMEMODE_LISTEN", "0.0.0.0:8797")
TOKEN = os.environ.get("GAMEMODE_TOKEN", "")
SSH_KEY = os.environ.get("GAMEMODE_SSH_KEY", str(Path.home() / ".ssh/id_ed25519"))
DISCORD_TOKEN = os.environ.get("SENTINEL_BOT_TOKEN", "")
DISCORD_USER = os.environ.get("DISCORD_USER_ID", "")
# Seconds. Boot with Vanguard, Tailscale and pending updates has been seen to
# take minutes; the grace is generous because a false restart costs a game.
BOOT_GRACE = int(os.environ.get("GAMEMODE_BOOT_GRACE", "600"))
HEARTBEAT_LOST = int(os.environ.get("GAMEMODE_HEARTBEAT_LOST", "300"))
RESCUE_WAIT = int(os.environ.get("GAMEMODE_RESCUE_WAIT", "600"))
OFF_WORDS = {"gaming off", "arch", "back to arch", "gamemode off"}

lock = threading.Lock()


def log(msg: str) -> None:
    print(f"[gamemode-relay] {msg}", file=sys.stderr, flush=True)


def load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {"active": False}


def save(s: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2))
    tmp.replace(STATE_FILE)


state = load()


# ---- Discord (REST only; DMs carry content without privileged intents) ----

def discord(method: str, path: str, body: dict | None = None):
    if not DISCORD_TOKEN:
        return None
    req = urllib.request.Request(
        f"https://discord.com/api/v10{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bot {DISCORD_TOKEN}",
            "Content-Type": "application/json",
            # Without a User-Agent Discord answers 403, which reads like a permissions problem.
            "User-Agent": "DiscordBot (gamemode-relay, 0.1)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"null")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log(f"discord {method} {path}: {exc}")
        return None


_dm_channel: str | None = None


def dm_channel() -> str | None:
    global _dm_channel
    if _dm_channel is None and DISCORD_USER:
        r = discord("POST", "/users/@me/channels", {"recipient_id": DISCORD_USER})
        if r:
            _dm_channel = r.get("id")
    return _dm_channel


def say(text: str) -> None:
    ch = dm_channel()
    if ch:
        discord("POST", f"/channels/{ch}/messages", {"content": text})
    log(f"say: {text}")


def poll_dm() -> None:
    ch = dm_channel()
    if not ch:
        return
    with lock:
        after = state.get("dm_after")
    q = f"?after={after}&limit=20" if after else "?limit=1"
    msgs = discord("GET", f"/channels/{ch}/messages{q}") or []
    if not msgs:
        return
    newest = max(int(m["id"]) for m in msgs)
    with lock:
        first_poll = not after
        state["dm_after"] = str(newest)
        save(state)
    if first_poll:
        return  # only messages written after the session was armed count
    for m in msgs:
        if m.get("author", {}).get("id") != DISCORD_USER:
            continue
        if m.get("content", "").strip().lower().rstrip(".!") in OFF_WORDS:
            request_off("discord DM")
            say("Got it: telling Windows to restart into Arch. Agents come back about 2 minutes after.")


# ---- session logic ----

def request_off(source: str) -> None:
    with lock:
        if state.get("active"):
            state["command"] = "off"
            state["off_requested"] = {"at": time.time(), "by": source}
            save(state)
    log(f"off requested by {source}")


# ---- finding Windows: no fixed LAN address (DHCP leases move) ----

def _arp() -> dict[str, str]:
    out = {}
    try:
        for line in Path("/proc/net/arp").read_text().splitlines()[1:]:
            f = line.split()
            if len(f) >= 4 and f[3] != "00:00:00:00:00:00":
                out.setdefault(f[3].lower(), f[0])
    except OSError:
        pass
    return out


def _sweep() -> None:
    r = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                       capture_output=True, text=True)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setblocking(False)
    for line in r.stdout.splitlines():
        ip = ipaddress.ip_interface(line.split()[3]).ip
        if not ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10"):
            continue
        for host in ipaddress.ip_network(f"{ip}/24", strict=False).hosts():
            try:
                s.sendto(b"\0", (str(host), 9))
            except OSError:
                pass
    s.close()
    time.sleep(2)


def windows_ip() -> str | None:
    """Where Windows is: the source of its last heartbeat, else its MAC in the
    neighbour table, else a /24 sweep and a second look."""
    with lock:
        seen, mac = state.get("windows_ip_seen"), (state.get("windows_mac") or "").lower()
    if seen:
        return seen
    if not mac:
        return None
    ip = _arp().get(mac)
    if not ip:
        _sweep()
        ip = _arp().get(mac)
    return ip


def ssh_restart(reason: str) -> bool:
    ip = windows_ip()
    with lock:
        user = state.get("ssh_user", "Korisnik")
    if not ip:
        log(f"ssh restart ({reason}): Windows not found by MAC")
        with lock:
            state.setdefault("rescues", []).append({"at": time.time(), "reason": reason, "ok": False,
                                                    "error": "not found"})
            save(state)
        return False
    cmd = [
        "ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new", f"{user}@{ip}",
        f'shutdown /r /f /t 5 /c "gamemode relay: {reason}"',
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        ok = r.returncode == 0
        log(f"ssh restart ({reason}): rc={r.returncode} {r.stderr.strip()[:200]}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        ok = False
        log(f"ssh restart ({reason}) failed: {exc}")
    with lock:
        state.setdefault("rescues", []).append({"at": time.time(), "reason": reason, "ok": ok})
        save(state)
    return ok


def watchdog() -> None:
    while True:
        time.sleep(20)
        with lock:
            s = dict(state)
        if not s.get("active"):
            continue
        now = time.time()
        try:
            poll_dm()
        except Exception as exc:  # the watchdog must outlive any Discord oddity
            log(f"poll_dm: {exc}")
        armed, last_hb = s.get("armed_at", now), s.get("last_heartbeat_at")
        rescues = s.get("rescues", [])
        last_rescue = rescues[-1]["at"] if rescues else 0
        if now - last_rescue < RESCUE_WAIT:
            continue
        reason = None
        if last_hb is None and now - armed > BOOT_GRACE:
            reason = f"no heartbeat {BOOT_GRACE}s after arming"
        elif last_hb is not None and now - last_hb > HEARTBEAT_LOST:
            reason = f"heartbeat lost for {int(now - last_hb)}s"
        elif s.get("hard_deadline") and now > s["hard_deadline"]:
            reason = "session hard deadline passed"
        if not reason:
            continue
        if len(rescues) >= 2:
            if not s.get("alerted"):
                say(f"gamemode needs a hand: archserver has not come back to Arch ({reason}), "
                    f"and {len(rescues)} SSH restarts did not fix it. A reset button press lands in Arch.")
                with lock:
                    state["alerted"] = True
                    save(state)
            continue
        ssh_restart(reason)


# ---- HTTP ----

class Handler(BaseHTTPRequestHandler):
    server_version = "gamemode-relay/0.1"

    def log_message(self, fmt, *args):  # quiet: one line per request is noise on a Pi
        pass

    def _send(self, code: int, obj: dict) -> None:
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            return {}

    def _auth(self) -> bool:
        return bool(TOKEN) and self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def do_GET(self):
        if self.path.rstrip("/") == "/status":
            with lock:
                s = {k: v for k, v in state.items() if k not in ("dm_after",)}
            return self._send(200, s)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            return self._send(401, {"error": "unauthorised"})
        body, path = self._body(), self.path.rstrip("/")
        if path == "/arm":
            with lock:
                state.clear()
                state.update(
                    active=True, armed_at=time.time(), mode=body.get("mode", "gaming"),
                    windows_mac=body.get("windows_mac"), windows_ip_seen=None,
                    ssh_user=body.get("ssh_user", "Korisnik"),
                    command=None, heartbeats=0, last_heartbeat_at=None, rescues=[],
                )
                if body.get("deadline_minutes"):
                    state["hard_deadline"] = time.time() + 60 * int(body["deadline_minutes"])
                save(state)
            threading.Thread(target=poll_dm, daemon=True).start()  # pins dm_after to "now"
            if body.get("announce", True):
                say(f"archserver is going into Windows ({state['mode']}). "
                    "DM me `gaming off` (or `arch`) any time to bring it back to Arch.")
            return self._send(200, {"ok": True})
        if path == "/heartbeat":
            with lock:
                if not state.get("active"):
                    # Windows booted without `gaming on` (by hand). Track it, command nothing.
                    state["passive_heartbeat_at"] = time.time()
                    save(state)
                    return self._send(200, {"command": None})
                first = state.get("last_heartbeat_at") is None
                state["last_heartbeat_at"] = time.time()
                state["windows_ip_seen"] = self.client_address[0]
                state["heartbeats"] = state.get("heartbeats", 0) + 1
                state["windows"] = {k: body.get(k) for k in (
                    "version", "phase", "streaming", "stream_reason", "upload_mbit",
                    "idle_seconds", "restart_reason", "tasks")}
                cmd = state.get("command")
                save(state)
            if first:
                log("first heartbeat from Windows")
            return self._send(200, {"command": cmd})
        if path == "/off":
            request_off(body.get("by", "http"))
            return self._send(200, {"ok": True})
        if path == "/disarm":
            with lock:
                was = dict(state)
                state.clear()
                state.update(active=False, last_session={
                    k: was.get(k) for k in ("mode", "armed_at", "heartbeats", "windows",
                                            "rescues", "off_requested")} | {"ended_at": time.time()})
                save(state)
            if was.get("active") and body.get("announce", True):
                mins = (time.time() - was.get("armed_at", time.time())) / 60
                why = (was.get("windows") or {}).get("restart_reason") or "unknown"
                say(f"archserver is back in Arch after {mins:.0f} min in Windows (reason: {why}).")
            return self._send(200, {"ok": True, "session": state["last_session"]})
        self._send(404, {"error": "not found"})


def main() -> int:
    if not TOKEN:
        log("GAMEMODE_TOKEN is not set; refusing to start")
        return 1
    host, port = LISTEN.rsplit(":", 1)
    threading.Thread(target=watchdog, daemon=True).start()
    log(f"listening on {LISTEN}; discord {'on' if DISCORD_TOKEN and DISCORD_USER else 'off'}")
    ThreadingHTTPServer((host, int(port)), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
