# gamemode

`gaming on` reboots archserver (an Arch desktop with an RTX 4060) into its existing Windows
install once, where a SYSTEM service starts Apollo so a laptop can stream games with Moonlight.
When nobody has streamed for a while, or you ask, Windows restarts and GRUB boots Arch again.
No monitor, keyboard or login is needed on archserver for any of this.

It has three parts, because they answer to three different machines:

| Part | Runs on | Job |
|---|---|---|
| `gaming` (Python) | archserver, Arch | Prepares Windows **offline** through the mounted partition, switches into Windows, archives what Windows reported on the way back. |
| `gamemoded.exe` (Go) | archserver, Windows | SYSTEM service: runs a task queue planted from Linux, writes reports to `C:\gamemode\reports`, restarts into Arch when idle, asked, or past its deadline. |
| `gamemode_relay.py` | pigion (always on) | Takes the service's heartbeats, carries `gaming off` from Discord while Linux is down, SSH-restarts Windows if the service goes quiet. |

## How it works

### Switching

```
gaming on
  ├─ refuses if an agent is busy, a GPU job runs, or docker is in use (--force overrides)
  ├─ refuses if Windows is first in BootOrder (a failure must fall back to Arch)
  ├─ writes C:\gamemode\mode.json   {"mode": "gaming", "idle_minutes": 20}
  ├─ arms the relay on pigion       (it DMs you that archserver went to Windows)
  ├─ efibootmgr --bootnext 0000     one shot; BootOrder and GRUB's default are never touched
  ├─ hotline --sleep <idle agents>  so they resume cleanly
  └─ systemctl reboot
```

Windows boots, `gamemoded` starts as an auto-start service, Apollo is already a service.
`gamemoded` measures whether a stream is running every 15 s and heartbeats to the relay.

Back to Arch happens when any of these fires; all of them end in a restart, and a restart lands in
Arch because GRUB is first in BootOrder. Before restarting, the service also sets a one-shot
`bcdedit /set {fwbootmgr} bootsequence {GRUB}` as a second guarantee.

| Trigger | Where |
|---|---|
| no stream for `idle_minutes` (default 20, counted from boot) | service |
| DM the bot `gaming off` (or `arch`) | relay → next heartbeat |
| `gaming off` from any machine with the config | relay → next heartbeat |
| "Back to Arch" shortcut / Ctrl+Alt+End / `gamemoded arch` | service, local |
| prep mode: task queue finished, or `prep_minutes` passed (30) | service |
| no heartbeat 10 min after arming, or heartbeats stop for 5 min | relay, over SSH |
| two SSH rescues failed | relay DMs you: press reset, it lands in Arch |

Back in Arch, `gamemode-boot.service` disarms the relay (which DMs you how long the session was and
why it ended) and copies `C:\gamemode\reports` to `~/.local/state/gamemode/sessions/<time>/`.

Windows booted by hand (no `mode.json`) is left alone: the service heartbeats and obeys an explicit
"Back to Arch", but never restarts on idle.

### Detecting a stream

Two signals, either one counts: Apollo's log saying `CLIENT CONNECTED` more recently than
`CLIENT DISCONNECTED` (and written in the last 10 min), or upload above 4 Mbit/s (`netstat -e`).
A false "streaming" only delays the wind-down; a false "idle" would cut off a game, so it leans
towards streaming. Windows Update or a Steam upload can keep the box up longer than it should.

### Preparing Windows offline

`gaming prepare` does everything that would otherwise need someone at the Windows desktop, from
Linux, through `/mnt/windows`:

- **SYSTEM hive:** adds the `gamemoded` service (auto start, LocalSystem, restart on failure),
  sets `HiberbootEnabled=0`, adds a firewall rule letting sshd answer the LAN on every profile (the
  stock rule is Private-only, and the wired network may be classed Public).
- **SOFTWARE hive:** a Defender exclusion for exactly `C:\gamemode\bin\gamemoded.exe`. An unsigned Go
  binary running as SYSTEM is what heuristics quarantine, and a quarantined service means Windows
  never restarts on its own.
- **`administrators_authorized_keys`:** appends archserver's and pigion's keys, in place.
- **`C:\gamemode`:** the service, the task queue, the Apollo installer, config and Apollo's web
  credentials.

Registry edits go through [hivex](https://libguestfs.org/hivex.3.html), the library virt-v2v uses to
inject services into Windows guests. The write discipline, because a broken SYSTEM hive means
Windows does not boot:

1. refuse a dirty hive (primary and secondary sequence numbers differ: the transaction log holds
   changes the file does not, and Windows would replay it over the edit);
2. back up both hives and the ESP to `/mnt/offload/gamemode-backup-<time>/`;
3. edit a scratch copy, commit, re-open it and verify every value;
4. copy the bytes over the original **in place**, so the file keeps its NTFS security descriptor
   (a rename would give it whatever ntfs3 invents for new files).

Only values that differ are written; a second `gaming prepare` changes nothing.

### The task queue: the feedback loop

`C:\gamemode\tasks\NN-name.ps1` run once each at the next boot, in name order, as SYSTEM, 12 min
cap each. Output lands in `reports\task-NN-name.{json,log}`, readable from Linux with
`gaming report [TASK]`. A changed script, or `gaming prepare --rerun NAME`, runs it again.
`gaming on --prep` boots Windows just to run the queue and come straight back.

| Task | Does |
|---|---|
| `00-acl` | Locks `C:\gamemode` to SYSTEM + Administrators (users read/execute), fixes the ACL sshd demands on the admin key file. |
| `01-inventory` | Read-only snapshot: OS, users, NICs, network profiles, services, firewall, power, GPU, monitors, firmware boot order, Defender, update state. |
| `02-sshd` | sshd automatic and running, plus a proper firewall rule (LAN + Tailscale). |
| `03-power` | Hibernation and Fast Startup off, no sleep on AC, Wake-on-LAN on the Realtek NIC. |
| `04-apollo` | Silent Apollo install, web credentials, `sunshine_name = archserver`. |
| `05-shortcuts` | The Windows adapter's user-facing half (below). |

### Finding hosts: by MAC, not by address

No LAN address is configured anywhere. DHCP leases move, so each side looks the other up by MAC:
cached IP if it still answers, else the neighbour table, else one UDP datagram to every address in
each local /24 (2.0 s measured) and a second look. Tailscale (`relay_fallbacks`) is the last resort.

| From → to | Works |
|---|---|
| Windows → pigion relay (`e4:5f:01:f1:e1:f4`) | yes, tested |
| pigion → Windows (`a8:a1:59:fd:4d:13`) | yes, tested; the last heartbeat's source address wins anyway |
| laptop → archserver | yes: the laptop's neighbour table shows the server's real MAC |
| archserver or pigion → laptop | **no**: the Wi-Fi extender rewrites client MACs to one locally administered address (`16:bf:ce:c3:96:d4`) on the wired side |

### The Windows adapter

The piece that lets you get back to Linux and the agents from inside Windows. Not a hotline port
(hotline needs tmux and systemd, and two operators would be two voices); just the ways back:

- **"Back to Arch"** on the public desktop and in the Start menu, hotkey **Ctrl+Alt+End**.
- **`gamemoded arch`** / **`gamemoded status`** in any shell (`C:\gamemode\bin` is on PATH).
- **DM the hotline bot `gaming off`** from Discord on any device: the relay on pigion polls that DM
  while a session is armed and Windows picks it up within one heartbeat (15 s).

Agents come back on their own once Arch is up: hotline's watchdog respawns the operator and
keepalive resumes the agents that `gaming on` put to sleep. While archserver is in Windows, hotline's
pigion front door answers "in Windows gaming mode" instead of trying to wake it.

## Install

```
ownbox install gamemode
```

Setup does the whole lifecycle: venv and CLI, the vendored hivex (Arch's package, extracted into
the checkout, nothing system-wide), the pinned Apollo installer, the cross-compiled service, the
relay on pigion (user unit `gamemode-relay`, port 8797), and the boot hook
(`/etc/systemd/system/gamemode-boot.service`, via sudo). `ownbox update gamemode` redoes all of that
and, if Windows was prepared before, re-plants changed files there; registry values are only
rewritten if they differ, with a backup first. `ownbox remove gamemode` removes the unit and relay,
and on a terminal asks before taking the service out of Windows (`gaming unprepare`; Apollo stays).

Then, once: `gaming prepare`.

On the laptop the same `ownbox install gamemode` takes the client role (no Windows partition there):
the CLI, plus Apollo's web credentials copied from archserver over SSH.

Machine-specific one-off tasks (a cleaning run, say) go in `~/.config/gamemode/tasks/` and are planted
with the repo's tasks; a task can raise its own time cap with `# gamemode-timeout: N` in its first lines.
Tasks named `*.always.ps1` run on every boot.

Requirements: Go (to build the service), `uv`, `efibootmgr`, passwordless `sudo` for
`efibootmgr --bootnext` and `systemctl reboot`, SSH to pigion, the Windows partition mounted
read-write (ntfs3) with no hibernation file.

Config: `~/.config/gamemode/config.toml` (created on first run), secrets in
`~/.config/gamemode/secrets.json` (0600, generated).

## Usage

| Command | Does |
|---|---|
| `gaming on` | Switch to Windows for streaming (refuses when busy; `--force`, `--dry-run`, `--no-reboot`). |
| `gaming on --prep [--deadline MIN]` | Boot Windows only to run the task queue, then come back. |
| `gaming off` | Ask Windows, via the relay, to restart into Arch. |
| `gaming status` | Firmware boot state, relay state, last Windows report, as JSON. |
| `gaming report [TASK]` | Summary of the last Windows boot, or one task's full output. |
| `gaming prepare [--dry-run] [--rerun TASK]` | Offline Windows prep (idempotent). |
| `gaming unprepare [--yes]` | Take the service, rule, exclusion, keys and `C:\gamemode` back out. |
| `gaming relay-deploy [--remove]` | Install/update the relay on pigion. |
| `gaming boot-hook` | What the boot unit runs. |
| `gaming pair [--wait S]` | (laptop) Pair Moonlight with Apollo: Moonlight's `--pin` plus Apollo's `/api/pin`, no typing. |
| `gaming bench [--seconds N] [--wait S]` | (laptop) Stream N s and record Moonlight's stats, ping under load and received bitrate. |

Exit codes: 0 success, 1 failure, 2 usage error or refused (busy, aborted).

## Measured

From the first real cycles on 2026-11-16/17 (archserver: RTX 4060, wired 1 GbE; laptop: Iris Xe on
5 GHz Wi-Fi at -69 dBm, Wi-Fi power saving off).

| What | Number |
|---|---|
| Prep boot, `gaming on --prep` to back in Arch, 6 tasks | 3.5 min (23:41:49 → 23:45:18) |
| Windows boot to first service heartbeat | ~30 s |
| Relay found by MAC from Windows | first try |
| Moonlight average network latency (1080p60, Desktop) | 3 ms, variance 1 ms |
| Frames dropped by the network / by jitter | 0.00% / 0.09% |
| Host processing (capture + NVENC) avg / min / max | 3.7 / 1.9 / 224 ms (max during a Defender scan) |
| Laptop decode / render time | 4.37 ms (software decode) / 1.53 ms |
| LAN ping server → laptop, 300 pings, Wi-Fi power save off | avg 3.2 ms, max 9.8 ms, 0 over 10 ms |
| Same with Wi-Fi power save on | avg 4.1 ms, 21/300 over 10 ms, 6/300 over 30 ms |
| Laptop ↔ server throughput over SSH (lower bound) | 122-168 Mbit/s direct; 42-54 over Tailscale |

| His Minecraft session (00:08-00:12Z, ~4 min, Moonlight asked 20 Mbit/s, H.264) | 18.7 Mbit/s upload at the last heartbeat |

The bench stream was an idle Windows desktop, so its 4.1 Mbit/s and 35 fps say nothing about a game
(Apollo only sends changed frames). For the one game session there is a single bitrate sample and no
Moonlight latency or frame stats: the Moonlight app he opened keeps no log. The service now records a
bitrate sample every 15 s (`gaming report` summarises it), so the next session gets a curve.

## Limitations

- **Game load is barely measured:** one bitrate sample from a 4-minute Minecraft session; no latency or
  frame stats under a game yet.
- **Software decode on the laptop in the first bench.** Moonlight's VA-API init failed (-1) when launched
  from a background user unit, although the Iris Xe has H.264/HEVC VA-API decode. The bench now forces
  the Wayland backends and records `hardware_decode`; whether that fixes it is untested.
- **No ICMP from Windows until the next boot:** Windows drops ping on the Public profile, so the first
  bench has no ping-under-load numbers. `02-sshd` now adds a LAN-only rule.
- **Login:** the Windows account here autologs in. On a machine with a password, Apollo streams the
  sign-in screen and you type it through Moonlight; this does not store passwords.
- **The laptop cannot be found by MAC** from the wired side (see the table above).
- **Windows Update** is left alone; any restart may apply staged updates and take longer.
- **Stream detection is a heuristic** (log line or upload rate); see "Detecting a stream".
- **Anyone at the Windows desktop** can restart it into Arch (`gamemoded arch` needs no elevation).
- **The Defender exclusion** covers one file in a directory locked to SYSTEM and Administrators.
- **pigion is a single point** for the remote paths (Discord `gaming off`, SSH rescue). If it is down,
  the service's own deadlines and the shortcut still work.
- **Firmware Wake-on-LAN** from power-off also needs the BIOS setting; the Windows side only arms the NIC.
- **PowerShell traps:** a helper function named like a built-in alias (`H`, `Mp`) silently never runs,
  because aliases win. `scripts/check-ps.ps1` (runs under pwsh on Linux) flags that and syntax errors;
  run it on any new task.
