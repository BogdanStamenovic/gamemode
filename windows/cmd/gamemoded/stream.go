package main

import (
	"io"
	"os"
	"strings"
	"time"
)

// Two independent signals for "a Moonlight client is streaming", because
// neither is trustworthy alone:
//   - Apollo's log: Sunshine prints "CLIENT CONNECTED" / "CLIENT DISCONNECTED".
//     Exact, but a crash mid-stream leaves "connected" as the last word.
//   - Upload rate: a stream is 10-80 Mbit/s up; an idle desktop is well under
//     1 Mbit/s. Robust, but Windows Update or a Steam upload also looks busy.
// Streaming = log says connected AND log was written recently, OR the rate is
// above cfg.StreamMbit. A false "streaming" only delays the wind-down; a false
// "idle" would cut off a game, so the OR leans the safe way.

type uploadMeter struct {
	last  uint64
	lastT time.Time
}

func newUploadMeter() *uploadMeter { return &uploadMeter{} }

func (m *uploadMeter) mbit() float64 {
	sent, ok := bytesSent()
	now := time.Now()
	defer func() { m.last, m.lastT = sent, now }()
	if !ok || m.lastT.IsZero() || sent < m.last {
		return 0
	}
	return float64(sent-m.last) * 8 / 1e6 / now.Sub(m.lastT).Seconds()
}

func apolloLogState(path string) (connected bool, fresh bool) {
	f, err := os.Open(path)
	if err != nil {
		return false, false
	}
	defer f.Close()
	fi, _ := f.Stat()
	size := fi.Size()
	const tail = 512 << 10
	if size > tail {
		f.Seek(size-tail, io.SeekStart)
	}
	b, _ := io.ReadAll(f)
	s := string(b)
	c := strings.LastIndex(s, "CLIENT CONNECTED")
	d := strings.LastIndex(s, "CLIENT DISCONNECTED")
	return c >= 0 && c > d, time.Since(fi.ModTime()) < 10*time.Minute
}

func detectStream(cfg Config, m *uploadMeter) (bool, string, float64) {
	rate := m.mbit()
	conn, fresh := apolloLogState(cfg.ApolloLog)
	switch {
	case conn && fresh:
		return true, "apollo log: client connected", rate
	case rate >= cfg.StreamMbit:
		return true, "upload rate", rate
	}
	return false, "", rate
}
