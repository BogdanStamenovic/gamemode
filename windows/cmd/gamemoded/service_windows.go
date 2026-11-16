package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"golang.org/x/sys/windows/svc"
)

type handler struct{}

func (handler) Execute(_ []string, req <-chan svc.ChangeRequest, status chan<- svc.Status) (bool, uint32) {
	status <- svc.Status{State: svc.StartPending}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { daemon(ctx); close(done) }()
	status <- svc.Status{State: svc.Running, Accepts: svc.AcceptStop | svc.AcceptShutdown}
	for c := range req {
		switch c.Cmd {
		case svc.Interrogate:
			status <- c.CurrentStatus
		case svc.Stop, svc.Shutdown:
			status <- svc.Status{State: svc.StopPending}
			cancel()
			select {
			case <-done:
			case <-time.After(10 * time.Second):
			}
			return false, 0
		}
	}
	return false, 0
}

func runService() int {
	os.MkdirAll(root, 0o755)
	if f, err := os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644); err == nil {
		log.SetOutput(f)
	}
	isSvc, err := svc.IsWindowsService()
	if err != nil || !isSvc {
		// Run in the foreground for debugging (from an elevated prompt).
		log.SetOutput(io.MultiWriter(os.Stderr, log.Writer()))
		daemon(context.Background())
		return 0
	}
	if err := svc.Run("gamemoded", handler{}); err != nil {
		log.Printf("svc.Run: %v", err)
		return 1
	}
	return 0
}

func cmdArch() int {
	cfg := defaultConfig()
	loadJSON(configPath, &cfg)
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Post("http://"+cfg.ListenAddr+"/arch", "text/plain", strings.NewReader(""))
	if err == nil && resp.StatusCode == 200 {
		fmt.Println("Restarting into Arch...")
		return 0
	}
	// The service is not answering. Do it directly; this needs no elevation
	// for an interactive user, and GRUB is first in BootOrder anyway.
	fmt.Println("gamemoded service not answering; restarting directly")
	runCmd(30*time.Second, "bcdedit", "/set", "{fwbootmgr}", "bootsequence", cfg.GrubGUID)
	out, err := runCmd(30*time.Second, "shutdown", "/r", "/t", "5", "/c", "gamemode: back to Arch")
	if err != nil {
		fmt.Println("shutdown failed:", err, out)
		return 1
	}
	return 0
}

func cmdStatus() int {
	cfg := defaultConfig()
	loadJSON(configPath, &cfg)
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get("http://" + cfg.ListenAddr + "/status")
	if err != nil {
		fmt.Println("gamemoded not answering:", err)
		return 1
	}
	defer resp.Body.Close()
	var m map[string]any
	json.NewDecoder(resp.Body).Decode(&m)
	b, _ := json.MarshalIndent(m, "", "  ")
	fmt.Println(string(b))
	return 0
}
