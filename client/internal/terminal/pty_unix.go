//go:build !windows

package terminal

import (
	"errors"
	"os"
	"os/exec"
	"os/signal"
	"sync"
	"syscall"

	"github.com/creack/pty"
	"golang.org/x/term"
)

func platformUnavailable() string { return "" }

type posixChild struct {
	command *exec.Cmd
	pty     *os.File
	closing sync.Once
}

func startChild(command []string, dir string, env []string, cols, rows int) (child, error) {
	cmd := exec.Command(command[0], command[1:]...)
	cmd.Dir = dir
	cmd.Env = env
	file, err := pty.StartWithSize(cmd, &pty.Winsize{Cols: uint16(cols), Rows: uint16(rows)})
	if err != nil {
		return nil, err
	}
	return &posixChild{command: cmd, pty: file}, nil
}

// Read reports the end as an error: Linux answers EIO once the agent and everything it
// started have exited.
func (c *posixChild) Read(buffer []byte) (int, error) { return c.pty.Read(buffer) }

func (c *posixChild) Write(data []byte) { _, _ = c.pty.Write(data) }

func (c *posixChild) Resize(cols, rows int) {
	_ = pty.Setsize(c.pty, &pty.Winsize{Cols: uint16(cols), Rows: uint16(rows)})
}

// Wait returns the exit status, or 128 + the signal that ended the agent, as shells do.
func (c *posixChild) Wait() int {
	err := c.command.Wait()
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		if status, ok := exit.Sys().(syscall.WaitStatus); ok && status.Signaled() {
			return 128 + int(status.Signal())
		}
		return exit.ExitCode()
	}
	if err != nil {
		return 1
	}
	return 0
}

func (c *posixChild) Close() { c.closing.Do(func() { _ = c.pty.Close() }) }

type posixConsole struct {
	saved *term.State
}

func newConsole() console { return &posixConsole{} }

func (c *posixConsole) Raw() error {
	state, err := term.MakeRaw(int(os.Stdin.Fd()))
	if err != nil {
		return err
	}
	c.saved = state
	return nil
}

func (c *posixConsole) Restore() {
	if c.saved != nil {
		_ = term.Restore(int(os.Stdin.Fd()), c.saved)
		c.saved = nil
	}
}

func (c *posixConsole) Watch(resized func(), stop <-chan struct{}) {
	changes := make(chan os.Signal, 1)
	signal.Notify(changes, syscall.SIGWINCH)
	defer signal.Stop(changes)
	for {
		select {
		case <-stop:
			return
		case <-changes:
			resized()
		}
	}
}
