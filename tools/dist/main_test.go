package main

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"
)

// The archives carry the server's own uninstallers, uninstall.sh with LF whatever the checkout's endings.
func TestWriteUninstaller(t *testing.T) {
	root, folder := t.TempDir(), t.TempDir()
	source := filepath.Join(root, "server", "agent_server", "install")
	if err := os.MkdirAll(source, 0o755); err != nil {
		t.Fatal(err)
	}
	for name, text := range map[string]string{"uninstall.sh": "#!/bin/sh\r\nset -eu\r\n", "uninstall.ps1": "param()\r\n"} {
		if err := os.WriteFile(filepath.Join(source, name), []byte(text), 0o644); err != nil {
			t.Fatal(err)
		}
		if err := writeUninstaller(root, folder, name); err != nil {
			t.Fatal(err)
		}
	}
	for name, want := range map[string]string{"uninstall.sh": "#!/bin/sh\nset -eu\n", "uninstall.ps1": "param()\r\n"} {
		got, err := os.ReadFile(filepath.Join(folder, name))
		if err != nil || string(got) != want {
			t.Errorf("%s: %q, %v; want %q", name, got, err, want)
		}
	}
	// The archives' install.sh too: a CR after `set -eu` stops sh with "Illegal option -".
	if got := string(scriptText("install.sh", []byte("#!/bin/sh\r\nset -eu\r\n"))); got != "#!/bin/sh\nset -eu\n" {
		t.Errorf("install.sh: %q", got)
	}
	if bytes.Contains(scriptText("install.sh", installSh), []byte("\r")) {
		t.Error("the embedded install.sh would go out with CR in it")
	}
	if got := string(scriptText("install.ps1", []byte("param()\r\n"))); got != "param()\r\n" {
		t.Errorf("install.ps1: %q", got)
	}
	// The real ones are where the build looks for them.
	for _, name := range []string{"uninstall.sh", "uninstall.ps1"} {
		if err := writeUninstaller(filepath.Join("..", ".."), t.TempDir(), name); err != nil {
			t.Errorf("the checkout's %s: %v", name, err)
		}
	}
}
