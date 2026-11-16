//go:build !windows

package main

import (
	"context"
	"fmt"
)

// Linux build: foreground only, for exercising the daemon loop against a fake root.
func runService() int { daemon(context.Background()); return 0 }
func cmdArch() int    { fmt.Println("arch: windows only"); return 1 }
func cmdStatus() int  { fmt.Println("status: windows only"); return 1 }
