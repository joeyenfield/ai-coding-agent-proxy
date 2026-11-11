//go:build !windows

package providers

import (
	"os"
	"path/filepath"
	"syscall"
)

// diskUsage is the filesystem path is on: where it's mounted, and its bytes in all, in use and
// free for this user (the root's reserve counts as neither used nor free, as df shows it).
func diskUsage(path string) (mount string, total, used, free uint64, ok bool) {
	var stat syscall.Statfs_t
	if err := syscall.Statfs(path, &stat); err != nil {
		return "", 0, 0, 0, false
	}
	// The fields' types differ between systems, so each is converted.
	size, blocks := uint64(stat.Bsize), uint64(stat.Blocks)
	if uint64(stat.Bfree) > blocks {
		return "", 0, 0, 0, false
	}
	// FreeBSD's count of blocks free for the user goes below zero on a disk filled into its reserve.
	available := uint64(stat.Bavail)
	if available > blocks {
		available = 0
	}
	return mountpoint(path), blocks * size, (blocks - uint64(stat.Bfree)) * size, available * size, true
}

// mountpoint is the highest folder above path on the same device.
func mountpoint(path string) string {
	for {
		parent := filepath.Dir(path)
		if parent == path {
			return path
		}
		here, there := device(path), device(parent)
		if here == nil || there == nil || *here != *there {
			return path
		}
		path = parent
	}
}

func device(path string) *uint64 {
	info, err := os.Stat(path)
	if err != nil {
		return nil
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return nil
	}
	id := uint64(stat.Dev)
	return &id
}
