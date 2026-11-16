package main

import (
	"os"
	"testing"
)

// GAMEMODE_TEST_RELAY_MAC=<mac> go test -run Live ./... resolves a real host on this LAN.
func TestLiveRelayByMAC(t *testing.T) {
	mac := os.Getenv("GAMEMODE_TEST_RELAY_MAC")
	if mac == "" {
		t.Skip("set GAMEMODE_TEST_RELAY_MAC")
	}
	root = t.TempDir()
	cachePath = root + "/relay-ip.txt"
	u := relayBase(Config{RelayMAC: mac, RelayPort: 8797}, true)
	t.Logf("resolved %s", u)
	if u == "" {
		t.Fatal("not found")
	}
}

func TestNormMAC(t *testing.T) {
	if normMAC("E4-5F-01-F1-E1-F4") != "e4:5f:01:f1:e1:f4" {
		t.Fatal(normMAC("E4-5F-01-F1-E1-F4"))
	}
	if m := arpLine.FindStringSubmatch("  192.168.1.8           e4-5f-01-f1-e1-f4     dynamic   "); m == nil || m[1] != "192.168.1.8" {
		t.Fatal(m)
	}
}
