package main

import (
	"bytes"
	"compress/gzip"
	"encoding/binary"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestPackageVersionFromGitDescribe(t *testing.T) {
	for describe, want := range map[string]struct {
		version string
		release int
	}{
		"v1.2.0":                   {"1.2.0", 1},
		"v1.2.0-3-gabcdef0":        {"1.2.0", 4},
		"1.10.2-12-g0123abc-dirty": {"1.10.2", 13},
		"56458f9-dirty":            {"0.0.0", 1},
		"0.4.0+56458f9.dirty":      {"0.4.0", 1},
		"0.4.0":                    {"0.4.0", 1},
		"dev":                      {"0.0.0", 1},
	} {
		version, release := PackageVersion(describe)
		if version != want.version || release != want.release {
			t.Errorf("%s: got %s-%d, want %s-%d", describe, version, release, want.version, want.release)
		}
	}
}

func testPackage(goarch string) *Package {
	return &Package{
		Name: "agent-machine", Summary: "test", Description: "test.", Version: "1.2.0", Release: 4, Describe: "v1.2.0-3-gabc",
		GoArch: goarch, Built: time.Unix(1700000000, 0),
		Files: []PackageFile{
			{Path: "/usr/lib/systemd/user/agent-machine.service", Mode: 0o644, Data: []byte(machineUnit)},
			{Path: "/usr/bin/agent-machine", Mode: 0o755, Data: []byte("#!/bin/sh\n")},
		},
	}
}

// Every Linux target gets every format; the real package managers are the test of the
// contents (see AGENTS.md), so this checks each one's outer layout.
func TestEveryLinuxTargetBuildsEveryFormat(t *testing.T) {
	for _, target := range Targets {
		goos, goarch, _ := strings.Cut(target, "/")
		if goos != "linux" {
			continue
		}
		for _, format := range LinuxFormats {
			data, err := testPackage(goarch).Build(format.Format)
			if err != nil {
				t.Fatalf("%s %s: %v", target, format.Format, err)
			}
			switch format.Format {
			case "deb":
				if !bytes.HasPrefix(data, []byte("!<arch>\ndebian-binary   ")) {
					t.Errorf("%s: not an ar archive starting with debian-binary", target)
				}
			case "rpm":
				if !bytes.HasPrefix(data, []byte{0xed, 0xab, 0xee, 0xdb}) || !bytes.Equal(data[96:99], []byte{0x8e, 0xad, 0xe8}) {
					t.Errorf("%s: no rpm lead and signature header", target)
				}
			case "apk", "arch":
				if _, err := gzip.NewReader(bytes.NewReader(data)); err != nil {
					t.Errorf("%s %s: not gzip: %v", target, format.Format, err)
				}
			}
		}
	}
}

func TestAlpinePackageLeavesOutTheSystemdUnit(t *testing.T) {
	data, err := testPackage("amd64").apk("x86_64")
	if err != nil {
		t.Fatal(err)
	}
	reader, _ := gzip.NewReader(bytes.NewReader(data))
	all, _ := io.ReadAll(reader) // reads every concatenated stream
	if bytes.Contains(all, []byte("systemd")) {
		t.Error("the apk carries the systemd unit")
	}
	if !bytes.Contains(all, []byte("usr/bin/agent-machine")) {
		t.Error("the apk is missing agent-machine")
	}
}

func TestRPMHeaderRegionPointsBackOverTheIndex(t *testing.T) {
	h := rpmHeader{}
	h.str(1000, "name")
	h.int32s(1006, 1)
	raw := h.bytes(rpmTagHeaderImmutable)
	count := binary.BigEndian.Uint32(raw[8:])
	size := binary.BigEndian.Uint32(raw[12:])
	if count != 3 || len(raw) != 16+16*int(count)+int(size) {
		t.Fatalf("index of %d entries, store of %d bytes, %d bytes in all", count, size, len(raw))
	}
	store := raw[16+16*count:]
	trailer := store[len(store)-16:]
	if binary.BigEndian.Uint32(trailer) != rpmTagHeaderImmutable || int32(binary.BigEndian.Uint32(trailer[8:])) != -48 {
		t.Errorf("trailer %x", trailer)
	}
}

func TestGUIDIsStableAndWellFormed(t *testing.T) {
	a, b := guid("upgrade:agent:amd64"), guid("upgrade:agent:amd64")
	if a != b || len(a) != 38 || a[15] != '5' || guid("upgrade:agent:arm64") == a {
		t.Errorf("guid %s", a)
	}
}

func TestBuildVersionComesFromTheVersionFile(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "VERSION"), []byte("1.4.2\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("AI_PROXY_VERSION", "")
	// No .git here: the commit is unknown.
	if got := BuildVersion(root); got != "1.4.2+unknown" {
		t.Errorf("got %q", got)
	}
	t.Setenv("AI_PROXY_VERSION", "2.0.0")
	if got := BuildVersion(root); got != "2.0.0" {
		t.Errorf("AI_PROXY_VERSION: got %q", got)
	}
}
