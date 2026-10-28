//go:build windows

package terminal

import (
	"os"
	"testing"
	"time"
)

func TestWindowsCommandRunsNpmShimsThroughCmd(t *testing.T) {
	shell := os.Getenv("COMSPEC")
	if shell == "" {
		shell = "cmd.exe"
	}
	application, line := windowsCommand([]string{`C:\Program Files\npm\opencode.cmd`, "--model", "a b"})
	// /s strips the outer quotes, leaving cmd the shim's own command line.
	if application != shell || line != escape(shell)+` /d /s /c ""C:\Program Files\npm\opencode.cmd" --model "a b""` {
		t.Fatalf("%q %q", application, line)
	}
	application, line = windowsCommand([]string{`C:\bin\claude.exe`})
	if application != `C:\bin\claude.exe` || line != `C:\bin\claude.exe` {
		t.Fatalf("%q %q", application, line)
	}
}

// A shell in the ConPTY reads the pseudo console, not the launcher's own stdin: agent-machine starts
// `agent --shell --headless` with its stdin redirected, and a shell that inherited that would read
// end-of-input and exit at once. Run with stdin redirected (go test ... < NUL) to see it.
func TestAShellInTheConsoleStaysOpenUntilItsTold(t *testing.T) {
	shell := os.Getenv("COMSPEC")
	if shell == "" {
		shell = "cmd.exe"
	}
	agent, err := startChild([]string{shell}, t.TempDir(), os.Environ(), 80, 24)
	if err != nil {
		t.Fatal(err)
	}
	defer agent.Close()
	go func() {
		// The console's output has to be read, or the shell blocks writing its prompt.
		buffer := make([]byte, 4096)
		for {
			if _, err := agent.Read(buffer); err != nil {
				return
			}
		}
	}()
	exited := make(chan int, 1)
	go func() { exited <- agent.Wait() }()
	select {
	case status := <-exited:
		t.Fatalf("the shell exited by itself (status %d): it read the launcher's stdin, not the console", status)
	case <-time.After(1500 * time.Millisecond):
	}
	agent.Write([]byte("exit\r"))
	select {
	case <-exited:
	case <-time.After(10 * time.Second):
		t.Fatal("the shell didn't exit when told to")
	}
}

func escape(path string) string {
	for _, char := range path {
		if char == ' ' || char == '\t' {
			return `"` + path + `"`
		}
	}
	return path
}
