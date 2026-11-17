//go:build !windows

package main

import (
	"os"
	"strconv"
	"strings"
)

// Linux debug build: transmitted bytes of non-loopback interfaces.
func bytesSent() (uint64, bool) {
	b, err := os.ReadFile("/proc/net/dev")
	if err != nil {
		return 0, false
	}
	var total uint64
	for _, line := range strings.Split(string(b), "\n")[2:] {
		name, rest, ok := strings.Cut(line, ":")
		if !ok || strings.TrimSpace(name) == "lo" {
			continue
		}
		f := strings.Fields(rest)
		if len(f) > 8 {
			n, _ := strconv.ParseUint(f[8], 10, 64)
			total += n
		}
	}
	return total, true
}
