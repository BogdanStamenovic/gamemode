package main

import (
	"fmt"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"time"
)

// Finding the relay by MAC instead of a fixed LAN address (his requirement:
// DHCP leases move). Order: cached IP if it still answers, the ARP table, then
// a sweep of every local IPv4 /24 to fill the ARP table and look again. The
// configured fallback URLs (Tailscale) come last.
//
// This works for pigion because it sits on the main LAN segment. It would NOT
// work for a host behind the Wi-Fi extender: the extender rewrites client MACs
// to one locally administered address (16:bf:ce:c3:96:d4), so the laptop's
// real MAC never appears on the wired side.

var cachePath = filepath.Join(root, "relay-ip.txt")

func normMAC(s string) string {
	return strings.ToLower(strings.NewReplacer("-", ":", ".", ":").Replace(strings.TrimSpace(s)))
}

var arpLine = regexp.MustCompile(`(?i)^\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f]{2}(?:[-:][0-9a-f]{2}){5})\s`)

func arpLookup(mac string) string {
	want := normMAC(mac)
	if runtime.GOOS == "linux" { // the debug build
		b, _ := os.ReadFile("/proc/net/arp")
		for _, line := range strings.Split(string(b), "\n") {
			if f := strings.Fields(line); len(f) >= 4 && normMAC(f[3]) == want {
				return f[0]
			}
		}
		return ""
	}
	out, err := runCmd(10*time.Second, "arp", "-a")
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(out, "\n") {
		if m := arpLine.FindStringSubmatch(line); m != nil && normMAC(m[2]) == want {
			return m[1]
		}
	}
	return ""
}

func localSubnets() []net.IP {
	var nets []net.IP
	ifs, _ := net.Interfaces()
	for _, i := range ifs {
		if i.Flags&net.FlagUp == 0 || i.Flags&net.FlagLoopback != 0 {
			continue
		}
		addrs, _ := i.Addrs()
		for _, a := range addrs {
			ipn, ok := a.(*net.IPNet)
			if !ok || ipn.IP.To4() == nil || !ipn.IP.IsPrivate() {
				continue // skips Tailscale's 100.64/10 CGNAT range too
			}
			nets = append(nets, ipn.IP.To4().Mask(net.CIDRMask(24, 32)))
		}
	}
	return nets
}

// sweep pokes every address in each local /24 with one UDP datagram so Windows
// ARPs for them; replies (or not) do not matter, the ARP table does.
func sweep() {
	for _, base := range localSubnets() {
		for h := 1; h < 255; h++ {
			ip := net.IPv4(base[0], base[1], base[2], byte(h))
			if c, err := net.DialTimeout("udp4", ip.String()+":9", 200*time.Millisecond); err == nil {
				c.Write([]byte{0})
				c.Close()
			}
		}
	}
	time.Sleep(2 * time.Second)
}

func tcpAlive(hostport string) bool {
	c, err := net.DialTimeout("tcp", hostport, 2*time.Second)
	if err != nil {
		return false
	}
	c.Close()
	return true
}

var resolvedRelay string

// relayBase returns the base URL for the relay, resolving by MAC when needed.
func relayBase(cfg Config, force bool) string {
	if cfg.RelayMAC == "" {
		return cfg.RelayURL
	}
	port := cfg.RelayPort
	if port == 0 {
		port = 8797
	}
	try := func(ip string) string {
		if ip == "" {
			return ""
		}
		hp := fmt.Sprintf("%s:%d", ip, port)
		if tcpAlive(hp) {
			return "http://" + hp
		}
		return ""
	}
	if !force && resolvedRelay != "" {
		return resolvedRelay
	}
	if b, err := os.ReadFile(cachePath); err == nil {
		if u := try(strings.TrimSpace(string(b))); u != "" {
			resolvedRelay = u
			return u
		}
	}
	ip := arpLookup(cfg.RelayMAC)
	if try(ip) == "" {
		sweep()
		ip = arpLookup(cfg.RelayMAC)
	}
	if u := try(ip); u != "" {
		os.WriteFile(cachePath, []byte(ip), 0o644)
		if u != resolvedRelay {
			event("relay found by MAC %s at %s", cfg.RelayMAC, ip)
		}
		resolvedRelay = u
		return u
	}
	for _, f := range cfg.RelayFallbacks {
		if tcpAlive(strings.TrimPrefix(strings.TrimPrefix(f, "http://"), "https://")) {
			resolvedRelay = f
			return f
		}
	}
	return cfg.RelayURL
}
