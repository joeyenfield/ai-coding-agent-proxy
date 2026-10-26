//go:build !linux && !windows

package providers

import "runtime"

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
