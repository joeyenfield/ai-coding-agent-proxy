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

// Group runs a program as the leader of its own process group, so StopTree ends what it starts too.
func Group(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

// Alive tells whether a process with this id is running and is one of ours (our user's).
//
// A process we may not signal (EPERM) is another user's, so it isn't the agent-machine, Ollama
// or llama.cpp we started, all of which run as us: it's a stale id from a status file whose
// process has gone and whose number was reused. On Android that's common: every app is its own
// user, and Android kills Termux's background processes, so agent-machine's id soon belongs to
// another app. Counting it as running made `agent-machine status` say running while the
// machine was offline, `start` refuse, and `stop` fail with "operation not permitted".
func Alive(pid int) bool {
	if pid <= 0 {
		return false
	}
	return syscall.Kill(pid, 0) == nil
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
