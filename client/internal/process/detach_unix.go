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
