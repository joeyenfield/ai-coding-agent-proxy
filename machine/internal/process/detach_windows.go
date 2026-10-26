//go:build windows

package process

import (
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"

	"golang.org/x/sys/windows"
)

// Detach runs the program without this console, so closing the terminal leaves it running.
func Detach(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{
		CreationFlags: windows.CREATE_NEW_PROCESS_GROUP | windows.DETACHED_PROCESS,
		HideWindow:    true,
	}
}

// Quiet runs a console program without a window of its own: agent-machine runs without a
// console, so Windows would otherwise open one for every docker call.
func Quiet(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: windows.CREATE_NO_WINDOW, HideWindow: true}
}

// Alive tells whether a process with this id is running.
func Alive(pid int) bool {
	if pid <= 0 {
		return false
	}
	handle, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return false
	}
	defer windows.CloseHandle(handle)
	var code uint32
	// 259 is STILL_ACTIVE.
	return windows.GetExitCodeProcess(handle, &code) == nil && code == 259
}

// Stop ends the process. Windows has no SIGTERM for a process without a console.
func Stop(pid int) error {
	process, err := os.FindProcess(pid)
	if err != nil {
		return err
	}
	return process.Kill()
}

// StopTree ends a program and the processes it started (Ollama's model runners, say).
func StopTree(pid int) error {
	cmd := exec.Command("taskkill", "/T", "/F", "/PID", strconv.Itoa(pid))
	Quiet(cmd)
	if output, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("taskkill: %v: %s", err, strings.TrimSpace(string(output)))
	}
	return nil
}
