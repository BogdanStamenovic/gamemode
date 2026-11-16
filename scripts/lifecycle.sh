#!/bin/sh
# ownbox lifecycle, role-aware. The same repo installs on two kinds of machine:
#   server: archserver itself (has the Windows partition with a registry):
#           hivex, Apollo installer, Windows service, relay on pigion, boot hook,
#           and re-planting Windows files if Windows was prepared before.
#   client: the laptop: only the CLI (gaming pair / bench / off), plus Apollo's
#           web credentials copied from archserver so pairing needs no typing.
set -eu
cd "$(dirname "$0")/.."
action=${1:?setup|update|remove}
hive=/mnt/windows/Windows/System32/config/SYSTEM
role=client
[ -f "$hive" ] && role=server
echo "gamemode: $action ($role role)" >&2

uv venv -q --allow-existing --python /usr/bin/python3 .venv
if [ "$action" != remove ]; then
  uv pip install -q -e . --python .venv/bin/python
fi

if [ "$role" = client ]; then
  sec=$HOME/.config/gamemode/secrets.json
  if [ "$action" != remove ] && [ ! -s "$sec" ]; then
    mkdir -p "$(dirname "$sec")"
    if ssh -o BatchMode=yes -o ConnectTimeout=5 archserver 'cat ~/.config/gamemode/secrets.json' > "$sec.tmp" 2>/dev/null; then
      chmod 600 "$sec.tmp" && mv "$sec.tmp" "$sec" && echo "copied Apollo credentials from archserver" >&2
    else
      rm -f "$sec.tmp"
      echo "archserver not reachable: copy ~/.config/gamemode/secrets.json from it before 'gaming pair'" >&2
    fi
  fi
  [ "$action" = remove ] && echo "kept ~/.config/gamemode (credentials)" >&2
  exit 0
fi

case $action in
  setup|update)
    scripts/fetch-deps.sh
    scripts/build-service.sh
    .venv/bin/gaming relay-deploy
    scripts/install-unit.sh
    if [ -d /mnt/windows/gamemode ]; then
      .venv/bin/gaming prepare
    else
      echo "Windows not prepared yet: run 'gaming prepare'" >&2
    fi
    ;;
  remove)
    scripts/install-unit.sh --remove
    .venv/bin/gaming relay-deploy --remove || true
    # Taking the service out of Windows writes the registry: asked on a TTY, skipped without one.
    if [ -t 0 ] && [ -d /mnt/windows/gamemode ]; then
      .venv/bin/gaming unprepare
    else
      echo "kept the Windows side; 'gaming unprepare --yes' removes it" >&2
    fi
    ;;
esac
