package process

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestWindowsDrivesInWSLAreSkippedWhenLookingForAProgram(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("WSL is Linux")
	}
	root := t.TempDir()
	windows, linux := filepath.Join(root, "c", "npm"), filepath.Join(root, "bin")
	for _, dir := range []string{windows, linux} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(windows, "opencode"), []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	mounts := filepath.Join(root, "mounts")
	line := `C:\134 ` + filepath.Join(root, "c") + ` 9p rw,noatime,aname=drvfs;path=C:\;uid=1000 0 0` + "\n"
	if err := os.WriteFile(mounts, []byte("proc /proc proc rw 0 0\n"+line), 0o644); err != nil {
		t.Fatal(err)
	}
	old := MountsFile
	MountsFile = mounts
	t.Cleanup(func() { MountsFile = old })
	t.Setenv("PATH", linux+string(os.PathListSeparator)+windows)

	if found, err := LookPath("opencode"); err == nil {
		t.Fatalf("found the Windows one: %s", found)
	}
	// A link in a Linux folder to it doesn't count either.
	if err := os.Symlink(filepath.Join(windows, "opencode"), filepath.Join(linux, "opencode")); err != nil {
		t.Fatal(err)
	}
	if found, err := LookPath("opencode"); err == nil {
		t.Fatalf("found it through a link: %s", found)
	}
	// A Linux build is found.
	os.Remove(filepath.Join(linux, "opencode"))
	if err := os.WriteFile(filepath.Join(linux, "opencode"), []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if found, err := LookPath("opencode"); err != nil || found != filepath.Join(linux, "opencode") {
		t.Fatalf("the Linux one: %q %v", found, err)
	}
	// Outside WSL (no drvfs mounts) it's plain exec.LookPath.
	if err := os.WriteFile(mounts, []byte("proc /proc proc rw 0 0\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := LookPath("opencode"); err != nil {
		t.Fatal(err)
	}
}

func TestAnAgentInItsOwnInstallFolderIsFound(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	t.Setenv("PATH", t.TempDir())
	name := "fake-agent"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	dir := filepath.Join(home, ".opencode", "bin")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, name), []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	found, err := LookPath("fake-agent")
	if err != nil || filepath.Dir(found) != dir {
		t.Fatalf("not found in ~/.opencode/bin: %q %v", found, err)
	}
	if _, err := LookPath("not-an-agent"); err == nil {
		t.Fatal("found something that isn't there")
	}
}
