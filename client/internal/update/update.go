// Package update replaces this machine's binaries with the build its agent-server offers
// (`agent update`), from the server's downloads (/downloads/index.json): the files in
// <os>-<arch>/artifact/ of the version the server runs, each checked against the
// SHA-256 the index lists before it replaces anything.
//
// A running program can't be overwritten on Windows, but it can be renamed, so each
// binary is swapped by renaming the old one to <name>.old and the new one into place.
// Cleanup removes the .old files when a program next starts. agent-machine has its own
// copy of this package: the two modules share no code.
package update

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"ai-coding-agent-proxy/client/internal/settings"
)

// Index is the part of the server's /downloads/index.json an update needs.
type Index struct {
	ServerVersion string `json:"server_version"`
	Version       string `json:"version"`
	Exact         bool   `json:"exact"`
	Hint          string `json:"hint"`
	Files         []File `json:"files"`
}

type File struct {
	Path   string `json:"path"`
	Name   string `json:"name"`
	Size   int64  `json:"size"`
	SHA256 string `json:"sha256"`
	URL    string `json:"url"`
}

// Step is one binary to replace: where it goes, and the file it comes from.
type Step struct {
	Program string
	Target  string
	File    File
}

// Fetch reads the server's downloads index. It's public, so no sign-in is sent.
func Fetch(server string) (*Index, error) {
	response, err := settings.ServerClient(30 * time.Second).Get(strings.TrimRight(server, "/") + "/downloads/index.json")
	if err != nil {
		return nil, fmt.Errorf("couldn't reach the agent-server at %s: %v", server, err)
	}
	defer response.Body.Close()
	data, err := io.ReadAll(response.Body)
	if err != nil {
		return nil, err
	}
	if response.StatusCode == http.StatusNotFound {
		return nil, fmt.Errorf("the agent-server at %s doesn't offer downloads (it's older than this client; update it first)", server)
	}
	if response.StatusCode >= 400 {
		return nil, fmt.Errorf("the agent-server at %s answered %d: %s", server, response.StatusCode, settings.Detail(data))
	}
	index := &Index{}
	if err := json.Unmarshal(data, index); err != nil {
		return nil, fmt.Errorf("unexpected downloads index from %s: %v", server, err)
	}
	return index, nil
}

// Plan picks each program's file for platform from the index, to replace the binary
// beside dir. Every program must be there: half an update would leave agent and
// agent-proxy at different versions.
func Plan(index *Index, platform, dir string, programs []string) ([]Step, error) {
	if index.Version == "" {
		hint := index.Hint
		if hint == "" {
			hint = "build them on the server with scripts/build-dist.sh"
		}
		return nil, fmt.Errorf("the agent-server (%s) has no builds to offer: %s", index.ServerVersion, hint)
	}
	extension := ""
	if strings.HasPrefix(platform, "windows-") {
		extension = ".exe"
	}
	var steps []Step
	for _, program := range programs {
		want := platform + "/artifact/" + program + extension
		var found *File
		for i := range index.Files {
			if index.Files[i].Path == want {
				found = &index.Files[i]
			}
		}
		if found == nil {
			return nil, fmt.Errorf("the agent-server's build %s has no %s for %s", index.Version, program, platform)
		}
		steps = append(steps, Step{Program: program, Target: filepath.Join(dir, program+extension), File: *found})
	}
	return steps, nil
}

// Apply downloads every step's file beside its target, checks it, then swaps them all in.
// Nothing is replaced unless every download checks out.
func Apply(server string, steps []Step) error {
	var ready []string
	defer func() {
		for _, path := range ready {
			_ = os.Remove(path)
		}
	}()
	for _, step := range steps {
		path, err := download(server, step)
		if err != nil {
			return err
		}
		ready = append(ready, path)
	}
	for i, step := range steps {
		if err := Swap(ready[i], step.Target); err != nil {
			return err
		}
	}
	ready = nil
	return nil
}

// download fetches a step's file to a hidden name beside its target and checks its SHA-256.
func download(server string, step Step) (string, error) {
	dir := filepath.Dir(step.Target)
	temporary, err := os.CreateTemp(dir, "."+filepath.Base(step.Target)+".new-*")
	if err != nil {
		return "", writable(dir, err)
	}
	path := temporary.Name()
	fail := func(err error) (string, error) {
		temporary.Close()
		os.Remove(path)
		return "", err
	}
	address := step.File.URL
	if !strings.Contains(address, "://") {
		address = strings.TrimRight(server, "/") + "/" + strings.TrimLeft(address, "/")
	}
	response, err := settings.ServerClient(10 * time.Minute).Get(address)
	if err != nil {
		return fail(fmt.Errorf("downloading %s: %v", step.File.Path, err))
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fail(fmt.Errorf("downloading %s: the agent-server answered %d", step.File.Path, response.StatusCode))
	}
	hash := sha256.New()
	if _, err := io.Copy(io.MultiWriter(temporary, hash), response.Body); err != nil {
		return fail(fmt.Errorf("downloading %s: %v", step.File.Path, err))
	}
	if sum := hex.EncodeToString(hash.Sum(nil)); !strings.EqualFold(sum, step.File.SHA256) {
		return fail(fmt.Errorf("%s doesn't match the agent-server's checksum (got %s, want %s); nothing was replaced", step.File.Path, sum, step.File.SHA256))
	}
	if err := temporary.Close(); err != nil {
		return fail(err)
	}
	if err := os.Chmod(path, 0o755); err != nil {
		return fail(err)
	}
	return path, nil
}

// Swap puts the file at fresh in target's place, keeping the old one as target.old: a
// running program can be renamed, not overwritten, on Windows. On other systems the old
// file is removed at once (the running program keeps its copy open).
func Swap(fresh, target string) error {
	old := target + ".old"
	if err := os.Remove(old); err != nil && !errors.Is(err, fs.ErrNotExist) {
		// An earlier .old still running (Windows): keep it, use another name.
		old = fmt.Sprintf("%s.old-%d", target, time.Now().UnixNano())
	}
	moved := false
	if _, err := os.Stat(target); err == nil {
		if err := os.Rename(target, old); err != nil {
			return writable(filepath.Dir(target), err)
		}
		moved = true
	}
	if err := os.Rename(fresh, target); err != nil {
		if moved {
			_ = os.Rename(old, target)
		}
		return writable(filepath.Dir(target), err)
	}
	if moved && runtime.GOOS != "windows" {
		_ = os.Remove(old)
	}
	return nil
}

// Cleanup removes what an earlier update left beside the programs in dir: the .old
// binaries, once they no longer run (Windows refuses until then), and half-done downloads.
func Cleanup(dir string, programs []string) {
	for _, program := range programs {
		for _, name := range []string{program, program + ".exe"} {
			for _, pattern := range []string{name + ".old", name + ".old-*", "." + name + ".new-*"} {
				matches, _ := filepath.Glob(filepath.Join(dir, pattern))
				for _, match := range matches {
					_ = os.Remove(match)
				}
			}
		}
	}
}

// writable turns a permission error into what to do about it: a binary installed by a
// package (/usr/bin, Program Files) belongs to the package manager.
func writable(dir string, err error) error {
	if errors.Is(err, fs.ErrPermission) {
		return fmt.Errorf("%s isn't writable here (%v): it was probably installed by a package; update it with the package manager or from the agent-server's Downloads page", dir, err)
	}
	return err
}

// Location is the folder of the running binary, links resolved.
func Location() (string, error) {
	self, err := os.Executable()
	if err != nil {
		return "", err
	}
	if resolved, err := filepath.EvalSymlinks(self); err == nil {
		self = resolved
	}
	return filepath.Dir(self), nil
}
