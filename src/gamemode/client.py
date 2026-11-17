"""Laptop side: find archserver by MAC, pair Moonlight with Apollo headlessly, measure a stream.

Pairing without clicking: Moonlight's CLI accepts the PIN it will present
(`moonlight pair HOST --pin NNNN`), and Apollo's web API accepts a PIN
(`POST /api/pin`). Both are driven from here, so nobody types anything.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import secrets
import signal
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import netfind
from .config import STATE_DIR, Config

APOLLO_HTTP = 47989  # Moonlight's HTTP port: answering means Apollo is up


class ClientError(Exception):
    pass


def moonlight_cmd() -> list[str]:
    for exe in ("moonlight", "moonlight-qt"):
        r = subprocess.run(["sh", "-c", f"command -v {exe}"], capture_output=True, text=True)
        if r.returncode == 0:
            return [r.stdout.strip()]
    r = subprocess.run(["flatpak", "info", "com.moonlight_stream.Moonlight"], capture_output=True, text=True)
    if r.returncode == 0:
        return ["flatpak", "run", "com.moonlight_stream.Moonlight"]
    raise ClientError("Moonlight is not installed (moonlight-qt or the Flatpak)")


def gui_env() -> dict[str, str]:
    """The graphical session's environment, so this works from ssh too."""
    env = dict(os.environ)
    r = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        k, _, v = line.partition("=")
        if k in ("WAYLAND_DISPLAY", "DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
                 "XDG_SESSION_TYPE", "XAUTHORITY") and k not in env:
            env[k] = v
    return env


def find_server(cfg: Config, wait: float = 0, log: Callable[[str], None] = print) -> str:
    deadline = time.time() + wait
    while True:
        ip = netfind.find(cfg.windows_mac, APOLLO_HTTP)
        if ip:
            return ip
        if time.time() >= deadline:
            raise ClientError(f"archserver ({cfg.windows_mac}) not answering on :{APOLLO_HTTP}")
        log("waiting for Apollo on archserver...")
        time.sleep(10)


class Apollo:
    """Minimal Apollo web-API client. Apollo serves a self-signed certificate."""

    def __init__(self, ip: str, user: str, password: str) -> None:
        self.base = f"https://{ip}:47990"
        self.user, self.password = user, password
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def _post(self, path: str, body: dict[str, Any], auth: bool = False) -> tuple[int, str]:
        headers = {"Content-Type": "application/json"}
        if auth:
            import base64
            tok = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            headers["Authorization"] = f"Basic {tok}"
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers=headers, method="POST")
        try:
            with self.opener.open(req, timeout=10) as r:
                return r.status, r.read().decode(errors="replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(errors="replace")

    def login(self) -> None:
        # Apollo uses a session cookie from /api/login; older Sunshine-style
        # builds take HTTP basic auth on every call instead. Try both.
        code, text = self._post("/api/login", {"username": self.user, "password": self.password})
        self.basic = code != 200
        if code not in (200, 404):
            raise ClientError(f"Apollo login: HTTP {code} {text[:200]}")

    def send_pin(self, pin: str, name: str) -> None:
        code, text = self._post("/api/pin", {"pin": pin, "name": name}, auth=self.basic)
        if code != 200 or '"status":false' in text.replace(" ", ""):
            raise ClientError(f"Apollo /api/pin: HTTP {code} {text[:200]}")


def paired(ip: str) -> bool:
    r = subprocess.run(moonlight_cmd() + ["list", ip, "--csv"], capture_output=True, text=True,
                       timeout=60, env=gui_env())
    return r.returncode == 0 and bool(r.stdout.strip())


def pair(cfg: Config, ip: str, log: Callable[[str], None]) -> None:
    if paired(ip):
        log(f"already paired with {ip}")
        return
    pin = f"{secrets.randbelow(10000):04d}"
    log(f"pairing with {ip} (PIN {pin})")
    proc = subprocess.Popen(moonlight_cmd() + ["pair", ip, "--pin", pin], env=gui_env(),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    api = Apollo(ip, cfg.apollo_user, cfg.apollo_password)
    api.login()
    err: Exception | None = None
    for _ in range(10):  # Apollo only accepts the PIN once Moonlight's request is pending
        time.sleep(2)
        try:
            api.send_pin(pin, os.uname().nodename)
            err = None
            break
        except ClientError as exc:
            err = exc
    try:
        out, _ = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        out = proc.communicate()[0]
    if err or proc.returncode != 0:
        raise ClientError(f"pairing failed: {err or ''} moonlight rc={proc.returncode}: {out[-400:]}")
    log("paired")


def _rx_bytes() -> int:
    total = 0
    for dev in Path("/sys/class/net").iterdir():
        if dev.name.startswith(("wl", "en", "eth")):
            total += int((dev / "statistics/rx_bytes").read_text())
    return total


def _ping(ip: str, count: int, interval: float) -> list[float]:
    r = subprocess.run(["ping", "-n", "-c", str(count), "-i", str(interval), ip],
                       capture_output=True, text=True, timeout=count * interval + 30)
    return [float(m) for m in re.findall(r"time=([\d.]+) ms", r.stdout)]


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    return s[min(len(s) - 1, int(p / 100 * len(s)))]


def bench(cfg: Config, seconds: int, app: str, bitrate_kbps: int, resolution: str, fps: int,
          log: Callable[[str], None]) -> dict[str, Any]:
    ip = find_server(cfg, wait=0, log=log)
    pair(cfg, ip, log)
    idle_rtt = _ping(ip, 50, 0.2)
    cmd = moonlight_cmd() + ["stream", ip, app, "--resolution", resolution, "--fps", str(fps),
                             "--bitrate", str(bitrate_kbps), "--display-mode", "borderless",
                             "--performance-overlay", "--no-quit-after"]
    log("streaming: " + " ".join(cmd[-14:]))
    logf = STATE_DIR / f"moonlight-{time.strftime('%Y%m%d-%H%M%S')}.log"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(logf, "w") as lf:
        proc = subprocess.Popen(cmd, env=gui_env(), stdout=lf, stderr=subprocess.STDOUT)
        time.sleep(8)  # session setup; not part of the measurement
        rtts: list[float] = []
        t = threading.Thread(target=lambda: rtts.extend(_ping(ip, int(seconds / 0.2), 0.2)))
        rx0, t0 = _rx_bytes(), time.time()
        t.start()
        t.join()
        rx1, t1 = _rx_bytes(), time.time()
        # End the session from the host side so Moonlight closes normally and
        # logs its "Global video stats"; SIGTERM only if that does not work.
        subprocess.run(moonlight_cmd() + ["quit", ip], env=gui_env(), capture_output=True, timeout=60)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
    text = logf.read_text(errors="replace")
    result = {
        "server": ip, "app": app, "resolution": resolution, "fps": fps,
        "requested_bitrate_mbit": bitrate_kbps / 1000,
        "measured_rx_mbit": round((rx1 - rx0) * 8 / 1e6 / (t1 - t0), 1),
        "seconds": round(t1 - t0, 1),
        "rtt_idle_ms": {"avg": round(sum(idle_rtt) / max(len(idle_rtt), 1), 2),
                        "p95": _pct(idle_rtt, 95), "max": max(idle_rtt, default=float("nan"))},
        "rtt_streaming_ms": {"avg": round(sum(rtts) / max(len(rtts), 1), 2), "p50": _pct(rtts, 50),
                             "p95": _pct(rtts, 95), "p99": _pct(rtts, 99),
                             "max": max(rtts, default=float("nan")), "n": len(rtts),
                             "over_10ms": sum(r > 10 for r in rtts)},
        "moonlight_log": str(logf),
        "moonlight_stats": [ln for ln in text.splitlines()
                            if re.search(r"(?i)latency|frames dropped|bitrate|decod|fps|stats", ln)][-40:],
        "moonlight_rc": proc.returncode,
        "hardware_decode": "Failed to initialize VAAPI" not in text and "VAAPI" in text,
    }
    if not rtts:
        result["rtt_note"] = "no ping replies: Windows firewall drops ICMP (needs the gamemode-icmp rule)"
    out = STATE_DIR / f"bench-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(result, indent=2))
    log(f"saved {out}")
    return result
