"""Client for the pigion relay (relay/gamemode_relay.py)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from . import netfind
from .config import Config


class RelayError(Exception):
    pass


def base_url(cfg: Config) -> str:
    ip = netfind.find(cfg.relay_mac, cfg.relay_port)
    if ip:
        return f"http://{ip}:{cfg.relay_port}"
    for url in cfg.relay_fallbacks:
        host, _, port = url.split("//", 1)[-1].partition(":")
        if netfind._alive(host, int(port or 80)):
            return url
    raise RelayError(f"relay not found by MAC {cfg.relay_mac} nor at {cfg.relay_fallbacks}")


def call(cfg: Config, method: str, path: str, body: dict[str, Any] | None = None,
         timeout: float = 8) -> dict[str, Any]:
    req = urllib.request.Request(
        base_url(cfg).rstrip("/") + path,
        data=json.dumps(body or {}).encode() if method == "POST" else None,
        method=method,
        headers={"Authorization": f"Bearer {cfg.relay_token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return dict(json.loads(r.read() or b"{}"))
    except urllib.error.HTTPError as exc:
        raise RelayError(f"{path}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RelayError(f"{path}: {exc}") from exc
