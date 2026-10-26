//go:build !windows

package process

import (
	"os/exec"
	"syscall"
)

// Detach runs the program in its own session, so closing the terminal leaves it running.
func Detach(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
}

// Quiet runs a program as usual; only Windows opens windows for console programs.
func Quiet(cmd *exec.Cmd) {}

// Alive tells whether a process with this id is running.
func Alive(pid int) bool {
	if pid <= 0 {
		return false
	}
	err := syscall.Kill(pid, 0)
	return err == nil || err == syscall.EPERM
}

// Stop asks the process to end (SIGTERM), so it can say goodbye to the server.
func Stop(pid int) error {
	return syscall.Kill(pid, syscall.SIGTERM)
}

// StopTree asks a program started with Detach, and what it started, to end: Detach made it
// the leader of its own process group, so the group gets the signal.
func StopTree(pid int) error {
	if err := syscall.Kill(-pid, syscall.SIGTERM); err != nil {
		return syscall.Kill(pid, syscall.SIGTERM)
	}
	return nil
}
