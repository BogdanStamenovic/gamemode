package main

import (
	"runtime"
	"testing"
)

func TestBytesSentLinux(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("linux debug counter only")
	}
	a, ok := bytesSent()
	if !ok || a == 0 {
		t.Fatalf("no counter: %d %v", a, ok)
	}
}
