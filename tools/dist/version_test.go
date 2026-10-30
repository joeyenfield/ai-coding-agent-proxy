package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// A clean tag is X.Y.Z; untracked files leave it so, a change to a tracked file makes it dirty.
func TestOnlyChangesToTrackedFilesMakeABuildDirty(t *testing.T) {
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
	write("app.go", "package app\n")
	run("init", "-q")
	run("add", ".")
	run("commit", "-q", "-m", "first")
	run("tag", "v1.4.2")
	if got := BuildVersion(root); got != "1.4.2" {
		t.Fatalf("clean tag: %s", got)
	}
	write("notes.txt", "untracked\n")
	if got := BuildVersion(root); got != "1.4.2" {
		t.Fatalf("with an untracked file: %s", got)
	}
	write("app.go", "package app // changed\n")
	if got := BuildVersion(root); !strings.HasPrefix(got, "1.4.2+") || !strings.HasSuffix(got, ".dirty") {
		t.Fatalf("with code changed: %s", got)
	}
}

// Untagged builds are X.Y.Z+<commit>, which says nothing about order, so packages count
// the commits since the tag: each build must be newer to apt and dnf than the one before.
func TestUntaggedBuildsGetARisingPackageRelease(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git isn't installed")
	}
	t.Setenv("AI_PROXY_VERSION", "")
	root := t.TempDir()
	run := func(args ...string) {
		cmd := exec.Command("git", append([]string{"-c", "user.name=t", "-c", "user.email=t@example.com"}, args...)...)
		cmd.Dir = root
		if output, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, output)
		}
	}
	commit := func(text string) {
		if err := os.WriteFile(filepath.Join(root, "app.go"), []byte(text), 0o644); err != nil {
			t.Fatal(err)
		}
		run("add", ".")
		run("commit", "-q", "-m", text)
	}
	if err := os.WriteFile(filepath.Join(root, "VERSION"), []byte("1.4.2\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	run("init", "-q")
	commit("package app\n")
	run("tag", "v1.4.2")
	if got := PackageRelease(root, BuildVersion(root)); got != 1 {
		t.Fatalf("tagged: release %d", got)
	}
	commit("package app // 1\n")
	commit("package app // 2\n")
	if got := PackageRelease(root, BuildVersion(root)); got != 3 {
		t.Fatalf("two commits after the tag: release %d", got)
	}
	// VERSION moved on before its tag exists: the new numbers already sort higher.
	if got := PackageRelease(root, "1.5.0+abc1234"); got != 1 {
		t.Fatalf("no tag yet: release %d", got)
	}
}
