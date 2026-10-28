package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// The console saves config/backends.yaml on the server's computer; that's settings, not code,
// and mustn't make the server's build (or a dist built there) dirty.
func TestTheServersOwnConfigDoesntMakeABuildDirty(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git isn't installed")
	}
	t.Setenv("AI_PROXY_VERSION", "")
	root := t.TempDir()
	write := func(name, text string) {
		path := filepath.Join(root, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	run := func(args ...string) {
		cmd := exec.Command("git", append([]string{"-c", "user.name=t", "-c", "user.email=t@example.com"}, args...)...)
		cmd.Dir = root
		if output, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, output)
		}
	}
	write("VERSION", "1.4.2\n")
	write("config/backends.yaml", "backends: {}\n")
	write("app.go", "package app\n")
	run("init", "-q")
	run("add", ".")
	run("commit", "-q", "-m", "first")
	run("tag", "v1.4.2")
	if got := BuildVersion(root); got != "1.4.2" {
		t.Fatalf("clean tag: %s", got)
	}
	write("config/backends.yaml", "backends: {saved: {}}\n")
	write("notes.txt", "untracked\n")
	if got := BuildVersion(root); got != "1.4.2" {
		t.Fatalf("with config changed: %s", got)
	}
	write("app.go", "package app // changed\n")
	if got := BuildVersion(root); !strings.HasPrefix(got, "1.4.2+") || !strings.HasSuffix(got, ".dirty") {
		t.Fatalf("with code changed: %s", got)
	}
}
