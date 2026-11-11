//go:build !linux && !windows

package providers

import (
	"context"
	"os"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
	"time"
)

func platform() string { return runtime.GOOS }

// cpuTimes isn't read on these systems: the CPU's use is left out.
func cpuTimes() (uint64, uint64, bool) { return 0, 0, false }

// memory is the installed memory only, from sysctl.
func memory() (uint64, uint64) {
	if runtime.GOOS == "darwin" {
		return 0, sysctl("hw.memsize")
	}
	return 0, sysctl("hw.physmem")
}

// output is what a system tool prints. There's no /proc here, so the tools are asked; one that
// hangs mustn't hold the sample.
func output(name string, args ...string) (string, bool) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	data, err := exec.CommandContext(ctx, name, args...).Output()
	return string(data), err == nil
}

func uptime() any {
	text, ok := output("sysctl", "-n", "kern.boottime")
	if !ok {
		return nil
	}
	boot, ok := parseBootTime(text)
	if !ok {
		return nil
	}
	return uptimeSince(boot)
}

func loadAverage() any {
	text, ok := output("sysctl", "-n", "vm.loadavg")
	if !ok {
		return nil
	}
	return parseLoad(text)
}

// swap is known on macOS only, which keeps it in one sysctl.
func swap() (used, total any) {
	if runtime.GOOS != "darwin" {
		return nil, nil
	}
	text, ok := output("sysctl", "-n", "vm.swapusage")
	if !ok {
		return nil, nil
	}
	return parseSwapUsage(text)
}

func network() any {
	text, ok := output("netstat", "-ibn")
	if !ok {
		return nil
	}
	totals, ok := parseNetstat(text)
	if !ok {
		return nil
	}
	return totals.answer()
}

// rss is agent-machine's resident memory, which ps prints in KiB.
func rss() any {
	text, ok := output("ps", "-o", "rss=", "-p", strconv.Itoa(os.Getpid()))
	if !ok {
		return nil
	}
	value, err := strconv.ParseUint(strings.TrimSpace(text), 10, 64)
	if err != nil {
		return nil
	}
	return value * 1024
}
