from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from gamemode import hive, netfind
from gamemode.cli import main

SAMPLE = Path("/boot/efi/EFI/Microsoft/Boot/BCD")  # a real, small regf hive


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--version"])
    assert "gaming" in capsys.readouterr().out


def test_usage_error_is_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GAMEMODE_CONFIG_DIR", str(tmp_path))
    assert main(["nonsense"]) == 2


def test_norm_mac() -> None:
    assert netfind.norm("A8-A1-59-FD-4D-13") == "a8:a1:59:fd:4d:13"


@pytest.mark.skipif(not SAMPLE.exists(), reason="no sample hive on this machine")
def test_hive_apply_and_delete_roundtrip(tmp_path: Path) -> None:
    h = tmp_path / "BCD"
    shutil.copyfile(SAMPLE, h)
    vals = [
        hive.Value("gamemode-test\\sub", "s", hive.REG_SZ, hive.sz("x y")),
        hive.Value("gamemode-test\\sub", "d", hive.REG_DWORD, hive.dword(7)),
    ]
    assert hive.apply(h, vals, lambda m: None) == 2
    assert hive.apply(h, vals, lambda m: None) == 0  # idempotent
    assert hive.read_value(h, "gamemode-test\\sub", "d") == (hive.REG_DWORD, hive.dword(7))
    assert hive.is_clean(h)
    assert hive.delete(h, [], [("gamemode-test\\sub", "s")], lambda m: None) == 1
    assert hive.read_value(h, "gamemode-test\\sub", "s") is None
    assert hive.read_value(h, "gamemode-test\\sub", "d") is not None
    assert hive.delete(h, ["gamemode-test"], [], lambda m: None) == 1
    assert not hive.key_exists(h, "gamemode-test")


def test_dirty_hive_refused(tmp_path: Path) -> None:
    h = tmp_path / "H"
    h.write_bytes(b"regf" + (1).to_bytes(4, "little") + (2).to_bytes(4, "little") + bytes(500))
    assert not hive.is_clean(h)
    with pytest.raises(hive.HiveError):
        hive.apply(h, [hive.Value("a", "b", hive.REG_DWORD, hive.dword(1))], lambda m: None)


def test_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GAMEMODE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(f'windows_mount = "{tmp_path}/win"\n')
    assert main(["-q", "report"]) == 1
    rep = tmp_path / "win/gamemode/reports"
    rep.mkdir(parents=True)
    (rep / "latest.json").write_text(json.dumps({"started": "t", "mode": {"mode": "prep"}}))
    (rep / "task-01-x.json").write_text(json.dumps({"name": "01-x", "exit_code": 0, "seconds": 1}))
    assert main(["report"]) == 0


def test_boot_hook_retries_relay(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from gamemode import boot, cli, relay

    monkeypatch.setenv("GAMEMODE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(f'windows_mount = "{tmp_path}/win"\n')
    calls = []

    def flaky(cfg, method, path, body=None, timeout=8):
        calls.append(path)
        if len(calls) < 3:
            raise relay.RelayError("network not up yet")
        return {"session": {"mode": "prep"}}

    monkeypatch.setattr(relay, "call", flaky)
    monkeypatch.setattr(boot, "clear_bootnext", lambda: None)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert main(["-q", "boot-hook"]) == 0
    assert calls == ["/disarm"] * 3


def test_sleep_agents_keeps_never_sleep(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A fake `hotline` with hotline's real semantics: `--sleep N now` drops N from
    never-sleep.json, `--sleep N never` adds it back, `--list` shows idle agents."""
    import os
    import stat

    from gamemode import boot

    state = tmp_path / "state"
    state.mkdir()
    listed = ["keeper-1", "keeper-2", "other-never"]
    (state / "never-sleep.json").write_text(json.dumps(listed))
    fake = tmp_path / "bin" / "hotline"
    fake.parent.mkdir()
    fake.write_text(f"""#!/usr/bin/env python3
import json, sys
f = {str(state / "never-sleep.json")!r}
a = sys.argv[1:]
if a == ["--list"]:
    print("keeper-1 (pid 1, /x)  idle")
    print("keeper-2 (pid 2, /x)  idle")
    print("plain-3 (pid 3, /x)  idle")
    print("busy-4 (pid 4, /x)  busy")
    print("hotline-80 (pid 5, /x)  idle oldest")
    sys.exit(0)
if a[0] == "--sleep" and len(a) == 3:
    s = set(json.load(open(f)))
    (s.discard if a[2] == "now" else s.add)(a[1])
    json.dump(sorted(s), open(f, "w"))
    open(f + ".log", "a").write(" ".join(a) + "\\n")
    sys.exit(0)
sys.exit(2)
""")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("HOTLINE_STATE", str(state))
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    monkeypatch.setattr(boot, "PATH_EXTRA", [str(fake.parent)])
    logs: list[str] = []
    boot.sleep_agents("self-0", logs.append)
    assert sorted(json.loads((state / "never-sleep.json").read_text())) == sorted(listed)
    calls = (state / "never-sleep.json.log").read_text().split("\n")
    assert "--sleep plain-3 now" in calls and "--sleep keeper-1 now" in calls
    assert not any("busy-4" in c or "hotline-80" in c for c in calls)
    assert not any(line.startswith("warning") for line in logs)
