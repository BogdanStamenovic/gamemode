"""Offline Windows registry edits through hivex.

hivex is not on PyPI. `gamemode`'s setup extracts the Arch `hivex` package into
`vendor/hivex` (no system install); this module loads it from there, or from a
system install if one exists.

Write discipline, because a broken SYSTEM hive means Windows does not boot:
  * refuse a dirty hive (primary != secondary sequence number: the transaction
    log holds changes the file does not, and Windows would replay the log over
    our edit at boot);
  * edit a scratch copy, commit, re-open the copy and verify every value;
  * only then copy the bytes over the original *in place* (open r+b), so the
    file keeps its NTFS security descriptor -- a rename would give it whatever
    descriptor ntfs3 invents for new files.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import struct
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REG_SZ, REG_EXPAND_SZ, REG_BINARY, REG_DWORD, REG_MULTI_SZ = 1, 2, 3, 4, 7

VENDOR = Path(__file__).resolve().parents[2] / "vendor" / "hivex" / "usr" / "lib"


class HiveError(Exception):
    pass


def _load_hivex() -> Any:
    try:
        import hivex

        return hivex
    except ImportError:
        pass
    lib = VENDOR / "libhivex.so.0"
    if not lib.exists():
        raise HiveError(f"hivex not found (expected {lib}); rerun `ownbox update gamemode`")
    # Preload the C library globally so the extension module's NEEDED entry
    # resolves without LD_LIBRARY_PATH, which must be set before process start.
    ctypes.CDLL(str(lib), mode=ctypes.RTLD_GLOBAL)
    for site in sorted(VENDOR.glob("python3*/site-packages")):
        sys.path.insert(0, str(site))
    try:
        import hivex
    except ImportError as exc:
        raise HiveError(f"vendored hivex does not load under this Python: {exc}") from exc
    return hivex


def sequence_numbers(path: Path) -> tuple[int, int]:
    with open(path, "rb") as f:
        head = f.read(12)
    if head[:4] != b"regf":
        raise HiveError(f"{path} is not a registry hive")
    return struct.unpack("<II", head[4:12])


def is_clean(path: Path) -> bool:
    a, b = sequence_numbers(path)
    return a == b


def sz(s: str) -> bytes:
    return (s + "\0").encode("utf-16-le")


def multi_sz(items: Iterable[str]) -> bytes:
    return ("\0".join(items) + "\0\0").encode("utf-16-le")


def dword(n: int) -> bytes:
    return struct.pack("<I", n)


@dataclass
class Value:
    key: str  # path below the hive root, backslash-separated
    name: str
    type: int
    data: bytes


def _node(h: Any, path: str, create: bool) -> Any:
    n = h.root()
    for part in [p for p in path.split("\\") if p]:
        child = h.node_get_child(n, part)
        if child is None:
            if not create:
                return None
            child = h.node_add_child(n, part)
        n = child
    return n


def read_value(path: Path, key: str, name: str) -> tuple[int, bytes] | None:
    hivex = _load_hivex()
    h = hivex.Hivex(str(path))
    n = _node(h, key, create=False)
    if n is None:
        return None
    # hivex's binding raises RuntimeError("Success") (errno 0) for a missing value.
    try:
        v = h.node_get_value(n, name)
    except RuntimeError:
        return None
    if not v:
        return None
    t, d = h.value_value(v)
    return t, d


def key_exists(path: Path, key: str) -> bool:
    hivex = _load_hivex()
    return _node(hivex.Hivex(str(path)), key, create=False) is not None


def current_control_set(system_hive: Path) -> str:
    v = read_value(system_hive, "Select", "Current")
    if v is None:
        raise HiveError("SYSTEM hive has no Select\\Current")
    return f"ControlSet{struct.unpack('<I', v[1][:4])[0]:03d}"


def apply(path: Path, values: list[Value], log: Callable[[str], None]) -> int:
    """Set `values` in the hive at `path`. Returns how many actually changed."""
    if not is_clean(path):
        raise HiveError(f"{path} is dirty (sequence numbers differ); boot Windows once cleanly first")
    hivex = _load_hivex()
    changed = [v for v in values if read_value(path, v.key, v.name) != (v.type, v.data)]
    if not changed:
        return 0
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
        work = Path(tmp) / path.name
        shutil.copyfile(path, work)
        h = hivex.Hivex(str(work), write=True)
        by_key: dict[str, list[Value]] = {}
        for v in changed:
            by_key.setdefault(v.key, []).append(v)
        for key, vals in by_key.items():
            n = _node(h, key, create=True)
            for v in vals:
                h.node_set_value(n, {"key": v.name, "t": v.type, "value": v.data})
                log(f"  {path.name}\\{key} : {v.name or '(default)'} = {_show(v)}")
        h.commit(None)
        del h
        for v in values:
            got = read_value(work, v.key, v.name)
            if got != (v.type, v.data):
                raise HiveError(f"verification failed for {v.key}\\{v.name}: {got!r}")
        if not is_clean(work):
            raise HiveError("hivex left the scratch hive dirty; not copying it back")
        data = work.read_bytes()
    with open(path, "r+b") as f:
        f.write(data)
        f.truncate()
        f.flush()
        os.fsync(f.fileno())
    return len(changed)


def _show(v: Value) -> str:
    if v.type in (REG_SZ, REG_EXPAND_SZ):
        return repr(v.data.decode("utf-16-le").rstrip("\0"))
    if v.type == REG_DWORD:
        return str(struct.unpack("<I", v.data)[0])
    if v.type == REG_MULTI_SZ:
        return repr([s for s in v.data.decode("utf-16-le").split("\0") if s])
    return v.data.hex()


def delete(path: Path, keys: list[str], values: list[tuple[str, str]],
           log: Callable[[str], None]) -> int:
    """Delete whole keys and single (key, name) values, same write discipline as apply()."""
    if not is_clean(path):
        raise HiveError(f"{path} is dirty (sequence numbers differ)")
    hivex = _load_hivex()
    todo_k = [k for k in keys if key_exists(path, k)]
    todo_v = [(k, n) for k, n in values if read_value(path, k, n) is not None]
    if not todo_k and not todo_v:
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / path.name
        shutil.copyfile(path, work)
        h = hivex.Hivex(str(work), write=True)
        for k in todo_k:
            h.node_delete_child(_node(h, k, create=False))
            log(f"  deleted key {path.name}\\{k}")
        for k, n in todo_v:
            node = _node(h, k, create=False)
            keep = []
            for v in h.node_values(node):
                if h.value_key(v) != n:
                    t, d = h.value_value(v)
                    keep.append({"key": h.value_key(v), "t": t, "value": d})
            h.node_set_values(node, keep)
            log(f"  deleted value {path.name}\\{k} : {n}")
        h.commit(None)
        del h
        if any(key_exists(work, k) for k in todo_k) or any(
                read_value(work, k, n) is not None for k, n in todo_v):
            raise HiveError("verification failed after delete")
        data = work.read_bytes()
    with open(path, "r+b") as f:
        f.write(data)
        f.truncate()
        f.flush()
        os.fsync(f.fileno())
    return len(todo_k) + len(todo_v)
