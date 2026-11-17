package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"
)

type Config struct {
	RelayURL       string   `json:"relay_url"` // last resort; normally found by MAC
	RelayMAC       string   `json:"relay_mac"` // pigion's MAC: the relay is located by it
	RelayPort      int      `json:"relay_port"`
	RelayFallbacks []string `json:"relay_fallbacks"` // e.g. the Tailscale URL
	RelayToken     string   `json:"relay_token"`     // shared secret with the relay
	IdleMinutes    int      `json:"idle_minutes"`    // gaming mode: restart after this long with no stream
	PrepMinutes    int      `json:"prep_minutes"`    // prep mode: hard deadline from service start
	TaskTimeout    int      `json:"task_timeout_minutes"`
	GrubGUID       string   `json:"grub_guid"`   // firmware entry for GRUB, used for the one-shot bootsequence
	StreamMbit     float64  `json:"stream_mbit"` // upload rate that counts as "a stream is running"
	ApolloLog      string   `json:"apollo_log"`
	ListenAddr     string   `json:"listen_addr"` // local control endpoint for `gamemoded arch`
}

func defaultConfig() Config {
	return Config{
		IdleMinutes: 20,
		PrepMinutes: 30,
		TaskTimeout: 12,
		GrubGUID:    "{dacb2679-a1a5-11f1-9015-806e6f6e6963}",
		StreamMbit:  4,
		ApolloLog:   `C:\Program Files\Apollo\config\sunshine.log`,
		ListenAddr:  "127.0.0.1:47800",
	}
}

// Mode is written by Linux before it reboots into Windows. Without a mode file
// the service is passive: Windows was booted by hand, so nothing restarts it.
type Mode struct {
	Mode            string `json:"mode"` // "prep" | "gaming"
	SetAt           string `json:"set_at"`
	DeadlineMinutes int    `json:"deadline_minutes,omitempty"`
	IdleMinutes     int    `json:"idle_minutes,omitempty"`
}

type TaskResult struct {
	Name     string  `json:"name"`
	ExitCode int     `json:"exit_code"`
	Seconds  float64 `json:"seconds"`
	TimedOut bool    `json:"timed_out"`
	Output   string  `json:"output"`
	Finished string  `json:"finished"`
}

type Sample struct {
	T         string  `json:"t"`
	Mbit      float64 `json:"mbit"`
	Streaming bool    `json:"streaming"`
}

type State struct {
	mu            sync.Mutex
	Version       string       `json:"version"`
	Started       time.Time    `json:"started"`
	Mode          Mode         `json:"mode"`
	Phase         string       `json:"phase"`
	Tasks         []TaskResult `json:"tasks"`
	Streaming     bool         `json:"streaming"`
	StreamReason  string       `json:"stream_reason"`
	UploadMbit    float64      `json:"upload_mbit"`
	LastStream    time.Time    `json:"last_stream"`
	IdleSeconds   int          `json:"idle_seconds"`
	RelayOK       bool         `json:"relay_ok"`
	RelayError    string       `json:"relay_error,omitempty"`
	RelayURL      string       `json:"relay_url,omitempty"`
	RestartReason string       `json:"restart_reason,omitempty"`
	// One sample per tick (15 s), so a whole play session has a bitrate curve.
	// 2880 samples = 12 h; older ones are dropped.
	Samples []Sample `json:"samples,omitempty"`
	Events  []string `json:"events"`
}

var st = &State{}

func event(format string, a ...any) {
	msg := fmt.Sprintf(format, a...)
	log.Print(msg)
	st.mu.Lock()
	st.Events = append(st.Events, time.Now().Format("15:04:05 ")+msg)
	if len(st.Events) > 200 {
		st.Events = st.Events[len(st.Events)-200:]
	}
	st.mu.Unlock()
}

func loadJSON(path string, v any) error {
	b, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	// Files written from Linux may carry a UTF-8 BOM if anyone edits them in Notepad.
	b = bytes.TrimPrefix(b, []byte{0xEF, 0xBB, 0xBF})
	return json.Unmarshal(b, v)
}

func writeJSON(path string, v any) {
	b, _ := json.MarshalIndent(v, "", "  ")
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err == nil {
		os.Rename(tmp, path)
	}
}

func snapshot() map[string]any {
	st.mu.Lock()
	defer st.mu.Unlock()
	b, _ := json.Marshal(st)
	var m map[string]any
	json.Unmarshal(b, &m)
	return m
}

// daemon is the service body. It returns only when ctx is cancelled.
func daemon(ctx context.Context) {
	os.MkdirAll(reportsDir, 0o755)
	cfg := defaultConfig()
	if err := loadJSON(configPath, &cfg); err != nil {
		event("config: %v (using defaults)", err)
	}
	var mode Mode
	if err := loadJSON(modePath, &mode); err != nil {
		mode = Mode{Mode: "passive"}
	}
	st.mu.Lock()
	st.Version, st.Started, st.Mode, st.Phase = version, time.Now(), mode, "starting"
	st.LastStream = time.Now() // the idle clock starts at boot
	st.mu.Unlock()
	event("gamemoded %s started, mode=%s", version, mode.Mode)

	bootStamp := time.Now().Format("20060102-150405")
	report := func() {
		writeJSON(filepath.Join(reportsDir, "boot-"+bootStamp+".json"), snapshot())
		writeJSON(filepath.Join(reportsDir, "latest.json"), snapshot())
	}
	report()

	restart := make(chan string, 4)
	go localServer(ctx, cfg, restart)

	// The hard deadline is armed before anything that could hang.
	if mode.Mode == "prep" {
		mins := mode.DeadlineMinutes
		if mins <= 0 {
			mins = cfg.PrepMinutes
		}
		go func() {
			select {
			case <-time.After(time.Duration(mins) * time.Minute):
				restart <- fmt.Sprintf("prep deadline of %d min reached", mins)
			case <-ctx.Done():
			}
		}()
	}

	go func() {
		setPhase("tasks")
		runTasks(ctx, cfg, report)
		report()
		if mode.Mode == "prep" {
			restart <- "prep tasks finished"
			return
		}
		setPhase("running")
	}()

	idleLimit := cfg.IdleMinutes
	if mode.IdleMinutes > 0 {
		idleLimit = mode.IdleMinutes
	}
	tick := time.NewTicker(15 * time.Second)
	defer tick.Stop()
	meter := newUploadMeter()
	for {
		select {
		case <-ctx.Done():
			report()
			return
		case why := <-restart:
			restartToArch(cfg, why, report)
			// Keep the loop alive; Windows is shutting down around us.
		case <-tick.C:
			streaming, reason, mbit := detectStream(cfg, meter)
			st.mu.Lock()
			st.Streaming, st.StreamReason, st.UploadMbit = streaming, reason, mbit
			st.Samples = append(st.Samples, Sample{time.Now().UTC().Format("15:04:05"), float64(int(mbit*10)) / 10, streaming})
			if len(st.Samples) > 2880 {
				st.Samples = st.Samples[len(st.Samples)-2880:]
			}
			if streaming {
				st.LastStream = time.Now()
			}
			idle := time.Since(st.LastStream)
			st.IdleSeconds = int(idle.Seconds())
			st.mu.Unlock()
			if mode.Mode == "gaming" && idleLimit > 0 && idle > time.Duration(idleLimit)*time.Minute {
				restart <- fmt.Sprintf("no stream for %d min", idleLimit)
			}
			if cmd := heartbeat(cfg); cmd == "off" {
				restart <- "relay asked for gaming off"
			}
			report()
		}
	}
}

func setPhase(p string) {
	st.mu.Lock()
	st.Phase = p
	st.mu.Unlock()
}

var restartOnce sync.Once

func restartToArch(cfg Config, why string, report func()) {
	restartOnce.Do(func() {
		event("restarting into Arch: %s", why)
		st.mu.Lock()
		st.RestartReason, st.Phase = why, "restarting"
		st.mu.Unlock()
		// Consume the mode: the next Windows boot is passive unless Linux sets it again.
		os.Rename(modePath, modePath+".used")
		if out, err := runCmd(30*time.Second, "bcdedit", "/set", "{fwbootmgr}", "bootsequence", cfg.GrubGUID); err != nil {
			event("bcdedit bootsequence failed (BootOrder still has GRUB first): %v %s", err, out)
		}
		if out, err := runCmd(30*time.Second, "bcdedit", "/enum", "{fwbootmgr}"); err == nil {
			event("fwbootmgr: %s", strings.Join(strings.Fields(out), " "))
		}
		heartbeat(cfg)
		report()
		if out, err := runCmd(30*time.Second, "shutdown", "/r", "/f", "/t", "5", "/d", "p:4:1", "/c", "gamemode: "+why); err != nil {
			event("shutdown failed: %v %s", err, out)
		}
		report()
	})
}

func runCmd(timeout time.Duration, name string, args ...string) (string, error) {
	// The Linux debug build must never reach the host's own shutdown(8).
	if runtime.GOOS != "windows" && (name == "shutdown" || name == "bcdedit") {
		event("dry-run: %s %s", name, strings.Join(args, " "))
		return "dry-run", nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	out, err := exec.CommandContext(ctx, name, args...).CombinedOutput()
	return string(out), err
}

// runTasks runs every C:\gamemode\tasks\*.ps1 that has no result yet, in name
// order. A task is retried on a later boot only if its result file is deleted.
func runTasks(ctx context.Context, cfg Config, report func()) {
	files, _ := filepath.Glob(filepath.Join(tasksDir, "*.ps1"))
	sort.Strings(files)
	for _, f := range files {
		if ctx.Err() != nil {
			return
		}
		name := strings.TrimSuffix(filepath.Base(f), ".ps1")
		resPath := filepath.Join(reportsDir, "task-"+name+".json")
		// NN-name.always.ps1 runs on every boot (e.g. re-locking ACLs on files
		// Linux dropped in since); everything else runs once.
		if _, err := os.Stat(resPath); err == nil && !strings.HasSuffix(name, ".always") {
			continue
		}
		event("task %s: start", name)
		t0 := time.Now()
		tctx, cancel := context.WithTimeout(ctx, time.Duration(taskTimeout(f, cfg.TaskTimeout))*time.Minute)
		cmd := exec.CommandContext(tctx, "powershell.exe", "-NoProfile", "-NonInteractive",
			"-ExecutionPolicy", "Bypass", "-File", f)
		cmd.Dir = root
		out, err := cmd.CombinedOutput()
		res := TaskResult{Name: name, Seconds: time.Since(t0).Seconds(), Finished: time.Now().Format(time.RFC3339)}
		if tctx.Err() == context.DeadlineExceeded {
			res.TimedOut, res.ExitCode = true, -1
		} else if err != nil {
			res.ExitCode = -2
			if ee, ok := err.(*exec.ExitError); ok {
				res.ExitCode = ee.ExitCode()
			}
		}
		cancel()
		o := string(out)
		if err != nil && o == "" {
			o = err.Error()
		}
		if len(o) > 60000 {
			o = o[:20000] + "\n...[truncated]...\n" + o[len(o)-40000:]
		}
		res.Output = o
		writeJSON(resPath, res)
		os.WriteFile(filepath.Join(reportsDir, "task-"+name+".log"), out, 0o644)
		st.mu.Lock()
		st.Tasks = append(st.Tasks, TaskResult{Name: res.Name, ExitCode: res.ExitCode, Seconds: res.Seconds, TimedOut: res.TimedOut})
		st.mu.Unlock()
		event("task %s: exit %d in %.0fs", name, res.ExitCode, res.Seconds)
		report()
	}
}

func heartbeat(cfg Config) string {
	base := relayBase(cfg, false)
	if base == "" {
		return ""
	}
	snap := snapshot()
	delete(snap, "samples") // the report keeps the curve; the Pi only needs the latest values
	body, _ := json.Marshal(snap)
	req, _ := http.NewRequest("POST", strings.TrimRight(base, "/")+"/heartbeat", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+cfg.RelayToken)
	client := &http.Client{Timeout: 8 * time.Second}
	resp, err := client.Do(req)
	st.mu.Lock()
	defer st.mu.Unlock()
	if err != nil {
		st.RelayOK, st.RelayError = false, err.Error()
		resolvedRelay = "" // re-resolve by MAC next tick
		return ""
	}
	defer resp.Body.Close()
	st.RelayURL = base
	var r struct {
		Command string `json:"command"`
	}
	json.NewDecoder(resp.Body).Decode(&r)
	st.RelayOK, st.RelayError = resp.StatusCode == 200, ""
	if resp.StatusCode != 200 {
		st.RelayError = resp.Status
	}
	return r.Command
}

// localServer is what `gamemoded arch` and the desktop shortcut talk to. It
// listens on loopback only; any local user may restart into Arch, which they
// could do with the Start menu anyway.
func localServer(ctx context.Context, cfg Config, restart chan<- string) {
	mux := http.NewServeMux()
	mux.HandleFunc("/status", func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(snapshot())
	})
	mux.HandleFunc("/arch", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" {
			http.Error(w, "POST", 405)
			return
		}
		restart <- "asked locally (gamemoded arch)"
		fmt.Fprintln(w, "restarting into Arch")
	})
	ln, err := net.Listen("tcp", cfg.ListenAddr)
	if err != nil {
		event("local endpoint: %v", err)
		return
	}
	srv := &http.Server{Handler: mux}
	go func() { <-ctx.Done(); srv.Close() }()
	srv.Serve(ln)
}

// taskTimeout reads an optional "# gamemode-timeout: N" (minutes) header from
// the first lines of a task, for the few that legitimately take long (a
// Defender scan); everything else gets the configured default.
func taskTimeout(path string, def int) int {
	b, err := os.ReadFile(path)
	if err != nil {
		return def
	}
	lines := strings.SplitN(string(b), "\n", 6)
	for _, l := range lines {
		var n int
		if _, err := fmt.Sscanf(strings.TrimSpace(l), "# gamemode-timeout: %d", &n); err == nil && n > 0 {
			return n
		}
	}
	return def
}
