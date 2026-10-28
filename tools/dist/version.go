package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

var baseVersion = regexp.MustCompile(`^\d+\.\d+\.\d+$`)

// BuildVersion is the version every component of this checkout is stamped with, from
// VERSION at the root: X.Y.Z when HEAD is tagged exactly vX.Y.Z and clean, else
// X.Y.Z+<short commit>, with .dirty for uncommitted changes outside config/ (Dirty), or
// X.Y.Z+unknown without git. AI_PROXY_VERSION wins. The server works it out the same way
// (server/agent_server/version.py), so it finds its own build in dist/; keep them in step.
func BuildVersion(root string) string {
	if value := strings.TrimSpace(os.Getenv("AI_PROXY_VERSION")); value != "" {
		return value
	}
	base := "0.0.0"
	if data, err := os.ReadFile(filepath.Join(root, "VERSION")); err == nil && baseVersion.MatchString(strings.TrimSpace(string(data))) {
		base = strings.TrimSpace(string(data))
	}
	describe, ok := git(root, "describe", "--tags", "--always", "--abbrev=7")
	if !ok {
		return base + "+unknown"
	}
	dirty := Dirty(root)
	if describe == "v"+base && !dirty {
		return base
	}
	commit, ok := git(root, "rev-parse", "--short=7", "HEAD")
	if !ok {
		commit = "unknown"
	}
	version := base + "+" + commit
	if dirty {
		version += ".dirty"
	}
	return version
}

// PackageRelease is the package release for a build of version: the commits since its
// tag plus one, so each untagged build (X.Y.Z+<commit>) is newer to apt and dnf than the
// tagged X.Y.Z and the builds before it. A tagged build, a version git can't place, or a
// VERSION whose tag doesn't exist yet is release 1.
func PackageRelease(root, version string) int {
	number, release := PackageVersion(version)
	if !strings.Contains(version, "+") {
		return release
	}
	count, ok := git(root, "rev-list", "--count", "v"+number+"..HEAD")
	if !ok {
		return 1
	}
	commits, err := strconv.Atoi(count)
	if err != nil {
		return 1
	}
	return commits + 1
}

// CleanExcludes are left out when deciding a checkout is dirty: config/ is the running
// server's own settings (the console saves backends.yaml), not its code. The server's
// version.py excludes the same.
var CleanExcludes = []string{":(exclude)config"}

// Dirty tells whether tracked files outside CleanExcludes have uncommitted changes;
// untracked files don't count.
func Dirty(root string) bool {
	_, changed := git(root, append([]string{"status", "--porcelain", "--untracked-files=no", "--", "."}, CleanExcludes...)...)
	return changed
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
