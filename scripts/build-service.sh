#!/bin/sh
# Cross-compile the Windows service. Pure Go, no cgo, so no Windows toolchain needed.
set -eu
cd "$(dirname "$0")/../windows"
ver=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' ../src/gamemode/__init__.py)
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -X main.version=$ver" -o ../build/gamemoded.exe ./cmd/gamemoded
echo "built build/gamemoded.exe ($ver)" >&2
