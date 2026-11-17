package main

import (
	"unsafe"

	"golang.org/x/sys/windows"
)

// bytesSent sums the 64-bit OutOctets of the physical, connected network
// adapters (GetIfTable2Ex). It replaced parsing `netstat -e`, whose counter is
// 32-bit and wrapped every ~2 min at stream rates, and which reported ~300
// Mbit/s median for a session Windows' own SRUM accounting put at 49 Mbit/s
// (2026-11-17). Virtual adapters (Hyper-V/WSL vEthernet, Tailscale, loopback)
// are excluded: the stream leaves through the physical NIC.
func bytesSent() (uint64, bool) {
	var table *windows.MibIfTable2
	if err := windows.GetIfTable2Ex(windows.MibIfEntryNormal, &table); err != nil {
		return 0, false
	}
	defer windows.FreeMibTable(unsafe.Pointer(table))
	rows := unsafe.Slice(&table.Table[0], table.NumEntries)
	var total uint64
	found := false
	for i := range rows {
		r := &rows[i]
		const hardwareInterface = 0x1 // InterfaceAndOperStatusFlags.HardwareInterface
		if r.InterfaceAndOperStatusFlags&hardwareInterface == 0 || r.OperStatus != 1 /* IfOperStatusUp */ {
			continue
		}
		total += r.OutOctets
		found = true
	}
	return total, found
}
