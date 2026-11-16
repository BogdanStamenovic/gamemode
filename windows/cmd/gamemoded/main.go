// gamemoded is the Windows half of gamemode: a SYSTEM service that runs the
// task queue planted from Linux, writes a report Linux can read back, keeps the
// machine from staying in Windows longer than it should, and restarts into
// Arch on request.
//
// Every path out of here ends in the same place: a restart. GRUB is first in
// the firmware BootOrder, so a restart lands in Arch without anything else
// being right. The one-shot bootsequence set before restarting is a second
// guarantee, not the first.
package main

import (
	"fmt"
	"os"
	"path/filepath"
)

// GAMEMODE_ROOT exists so the daemon can be exercised on Linux against a fake tree.
var root = envOr("GAMEMODE_ROOT", `C:\gamemode`)

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

var (
	configPath = filepath.Join(root, "config.json")
	modePath   = filepath.Join(root, "mode.json")
	tasksDir   = filepath.Join(root, "tasks")
	reportsDir = filepath.Join(root, "reports")
	logPath    = filepath.Join(root, "gamemoded.log")
)

func usage() {
	fmt.Fprintln(os.Stderr, `usage: gamemoded <command>
  arch      restart into Arch (asks the service; falls back to doing it directly)
  status    print the service's current state
  service   run as the Windows service (the SCM does this)
  version   print the version`)
}

var version = "dev"

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	switch os.Args[1] {
	case "service":
		os.Exit(runService())
	case "arch":
		os.Exit(cmdArch())
	case "status":
		os.Exit(cmdStatus())
	case "version":
		fmt.Println(version)
	default:
		usage()
		os.Exit(2)
	}
}
