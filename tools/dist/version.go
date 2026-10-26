package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
)

var baseVersion = regexp.MustCompile(`^\d+\.\d+\.\d+$`)

// BuildVersion is the version every component of this checkout is stamped with, from
// VERSION at the root: X.Y.Z when HEAD is tagged exactly vX.Y.Z and clean, else
// X.Y.Z+<short commit>, with .dirty for uncommitted changes, or X.Y.Z+unknown without
// git. AI_PROXY_VERSION wins. The server works it out the same way
// (server/agent_server/version.py), so it finds its own build in dist/; keep them in step.
func BuildVersion(root string) string {
	if value := strings.TrimSpace(os.Getenv("AI_PROXY_VERSION")); value != "" {
		return value
	}
	base := "0.0.0"
	if data, err := os.ReadFile(filepath.Join(root, "VERSION")); err == nil && baseVersion.MatchString(strings.TrimSpace(string(data))) {
		base = strings.TrimSpace(string(data))
	}
	describe, ok := git(root, "describe", "--tags", "--always", "--dirty", "--abbrev=7")
	if !ok {
		return base + "+unknown"
	}
	if describe == "v"+base {
		return base
	}
	commit, ok := git(root, "rev-parse", "--short=7", "HEAD")
	if !ok {
		commit = "unknown"
	}
	version := base + "+" + commit
	if strings.HasSuffix(describe, "-dirty") {
		version += ".dirty"
	}
	return version
}

func git(root string, args ...string) (string, bool) {
	if _, err := os.Stat(filepath.Join(root, ".git")); err != nil {
		return "", false
	}
	cmd := exec.Command("git", args...)
	cmd.Dir = root
	output, err := cmd.Output()
	text := strings.TrimSpace(string(output))
	return text, err == nil && text != ""
}
