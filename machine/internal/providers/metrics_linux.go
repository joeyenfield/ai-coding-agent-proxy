package providers

import (
	"os"
	"strconv"
	"strings"
)

func platform() string {
	release, _ := os.ReadFile("/proc/sys/kernel/osrelease")
	return strings.TrimSpace("Linux " + strings.TrimSpace(string(release)))
}

// cpuTimes is the idle and total jiffies from /proc/stat's first line.
func cpuTimes() (uint64, uint64, bool) {
	data, err := os.ReadFile("/proc/stat")
	if err != nil {
		return 0, 0, false
	}
	line, _, _ := strings.Cut(string(data), "\n")
	fields := strings.Fields(line)
	if len(fields) < 5 || fields[0] != "cpu" {
		return 0, 0, false
	}
	var idle, all uint64
	for i, field := range fields[1:] {
		value, _ := strconv.ParseUint(field, 10, 64)
		all += value
		// idle and iowait
		if i == 3 || i == 4 {
			idle += value
		}
	}
	return idle, all, true
}

// meminfo is /proc/meminfo's amounts, in bytes.
func meminfo() map[string]uint64 {
	data, err := os.ReadFile("/proc/meminfo")
	if err != nil {
		return nil
	}
	values := map[string]uint64{}
	for _, line := range strings.Split(string(data), "\n") {
		name, rest, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		if fields := strings.Fields(rest); len(fields) > 0 {
			value, _ := strconv.ParseUint(fields[0], 10, 64)
			values[name] = value * 1024
		}
	}
	return values
}

// memory is the bytes in use and in all, from /proc/meminfo.
func memory() (uint64, uint64) {
	values := meminfo()
	total, available := values["MemTotal"], values["MemAvailable"]
	if available == 0 || available > total {
		return 0, total
	}
	return total - available, total
}

// swap is the swap in use and in all; both 0 on a system without any.
func swap() (used, total any) {
	values := meminfo()
	if values == nil {
		return nil, nil
	}
	all, free := values["SwapTotal"], values["SwapFree"]
	if free > all {
		return nil, all
	}
	return all - free, all
}

func uptime() any {
	data, err := os.ReadFile("/proc/uptime")
	if err != nil {
		return nil
	}
	fields := strings.Fields(string(data))
	if len(fields) == 0 {
		return nil
	}
	return maybeNumber(fields[0])
}

func loadAverage() any {
	data, err := os.ReadFile("/proc/loadavg")
	if err != nil {
		return nil
	}
	return parseLoad(string(data))
}

func network() any {
	data, err := os.ReadFile("/proc/net/dev")
	if err != nil {
		return nil
	}
	totals, ok := parseNetDev(string(data))
	if !ok {
		return nil
	}
	return totals.answer()
}

// rss is agent-machine's resident memory: the second number of /proc/self/statm, in pages.
func rss() any {
	data, err := os.ReadFile("/proc/self/statm")
	if err != nil {
		return nil
	}
	fields := strings.Fields(string(data))
	if len(fields) < 2 {
		return nil
	}
	pages, err := strconv.ParseUint(fields[1], 10, 64)
	if err != nil {
		return nil
	}
	return pages * uint64(os.Getpagesize())
}
