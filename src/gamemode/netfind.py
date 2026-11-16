"""Find a LAN host by MAC address: neighbour table, then a /24 sweep, with a cache.

Works for hosts on the main LAN segment (archserver, pigion). Does NOT work for
hosts behind the Wi-Fi extender: it rewrites client MACs to one locally
administered address, so e.g. the laptop's real MAC never shows up from the
wired side. The relay script carries its own copy of this (it is one file).
"""

from __future__ import annotations

import ipaddress
import json
import socket
import subprocess
import time
from pathlib import Path

from .config import STATE_DIR

CACHE = STATE_DIR / "mac-cache.json"


def norm(mac: str) -> str:
    return mac.strip().lower().replace("-", ":")


def neighbours() -> dict[str, str]:
    """MAC -> IP from /proc/net/arp (IPv4, complete entries only)."""
    out: dict[str, str] = {}
    try:
        lines = Path("/proc/net/arp").read_text().splitlines()[1:]
    except OSError:
        return out
    for line in lines:
        f = line.split()
        if len(f) >= 4 and f[3] != "00:00:00:00:00:00":
            out.setdefault(norm(f[3]), f[0])
    return out


def local_24s() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    r = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                       capture_output=True, text=True)
    nets = []
    for line in r.stdout.splitlines():
        addr = line.split()[3]
        ip = ipaddress.ip_interface(addr).ip
        if ip.is_private and ipaddress.ip_address(ip) not in ipaddress.ip_network("100.64.0.0/10"):
            nets.append(ipaddress.ip_network(f"{ip}/24", strict=False))
    return nets


def sweep() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setblocking(False)
    for net in local_24s():
        for host in net.hosts():
            try:
                s.sendto(b"\0", (str(host), 9))
            except OSError:
                pass
    s.close()
    time.sleep(2)


def _alive(ip: str, port: int | None) -> bool:
    if port is None:
        return True
    try:
        with socket.create_connection((ip, port), timeout=2):
            return True
    except OSError:
        return False


def find(mac: str, port: int | None = None) -> str | None:
    """IP of `mac`, checked against `port` if given. Cached; re-scans when stale."""
    mac = norm(mac)
    try:
        cache = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        cache = {}
    ip = cache.get(mac)
    if ip and _alive(ip, port) and neighbours().get(mac, ip) == ip:
        return str(ip)
    ip = neighbours().get(mac)
    if not ip or not _alive(ip, port):
        sweep()
        ip = neighbours().get(mac)
    if ip and _alive(ip, port):
        cache[mac] = ip
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache))
        return ip
    return None
