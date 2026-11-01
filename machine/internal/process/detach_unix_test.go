//go:build !windows

package process

import (
	"os"
	"testing"
)

func TestAliveIsOursOnly(t *testing.T) {
	if !Alive(os.Getpid()) {
		t.Fatal("this process isn't alive")
	}
	if Alive(0) || Alive(-1) {
		t.Fatal("no process has id 0 or below")
	}
	if os.Getuid() == 0 {
		t.Skip("root may signal every process")
	}
	// init is root's: we may not signal it (EPERM), so it's no process of ours. On Android a stale
	// status file's id is often another app's process like this, and must read as not running.
	if Alive(1) {
		t.Fatal("another user's process counted as ours")
	}
}
