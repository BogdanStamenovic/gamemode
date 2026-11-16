#!/bin/sh
# Fetch what gamemode needs but nobody packages in a usable way, into the
# checkout only (nothing system-wide):
#   vendor/hivex       Arch's hivex package, extracted (offline registry edits)
#   .cache/Apollo-*.exe the Apollo installer that Windows installs from C:\gamemode\dl
set -eu
cd "$(dirname "$0")/.."
HIVEX=hivex-1.3.24-9-x86_64.pkg.tar.zst
APOLLO_VER=0.4.6
APOLLO_SHA=42b2aefaacb3474511517a56b96ee9f0517f30ac38b5dd2fda9fd5b478f5021a

if [ ! -e vendor/hivex/usr/lib/libhivex.so.0 ]; then
  mirror=$(sed -n 's/^Server = //p' /etc/pacman.d/mirrorlist | head -1 | sed 's/\$repo/extra/; s/\$arch/x86_64/')
  mkdir -p vendor/hivex .cache
  echo "fetching $HIVEX from $mirror" >&2
  curl -fsSL -o ".cache/$HIVEX" "$mirror/$HIVEX"
  bsdtar -C vendor/hivex -xf ".cache/$HIVEX"
fi

f=.cache/Apollo-$APOLLO_VER.exe
if [ ! -e "$f" ] || ! echo "$APOLLO_SHA  $f" | sha256sum -c --quiet 2>/dev/null; then
  mkdir -p .cache
  echo "fetching Apollo $APOLLO_VER" >&2
  curl -fsSL -o "$f" "https://github.com/ClassicOldSong/Apollo/releases/download/v$APOLLO_VER/Apollo-$APOLLO_VER.exe"
  echo "$APOLLO_SHA  $f" | sha256sum -c --quiet
fi
