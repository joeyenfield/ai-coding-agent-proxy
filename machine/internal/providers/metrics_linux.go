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

// memory is the bytes in use and in all, from /proc/meminfo.
func memory() (uint64, uint64) {
	data, err := os.ReadFile("/proc/meminfo")
	if err != nil {
		return 0, 0
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
	total, available := values["MemTotal"], values["MemAvailable"]
	if available == 0 || available > total {
		return 0, total
	}
	return total - available, total
}
