//go:build windows

package terminal

import (
	"os"
	"testing"
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

func escape(path string) string {
	for _, char := range path {
		if char == ' ' || char == '\t' {
			return `"` + path + `"`
		}
	}
	return path
}
