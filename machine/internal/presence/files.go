package presence

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"unicode/utf8"

	"ai-coding-agent-proxy/machine/internal/settings"
)

// FilesLimit is the most entries one listing returns; a folder with more says it's truncated.
const FilesLimit = 1000

// FileReadLimit is the most of a file one read sends; a longer file says it's truncated.
const FileReadLimit = 1 << 20

// FilesRequest is the server's {"type": "files"}: op "list" (the folders and files inside one
// folder) or "read" (one file's text), for the console's file viewer.
type FilesRequest struct {
	Type string `json:"type"`
	ID   string `json:"request"`
	Op   string `json:"op"`
	Path string `json:"path"`
}

// Files answers a files request, as the files_result to send back. Only while files are on
// (agent-machine files on), only inside the roots (checked here after resolving links, whatever
// the server allowed), and never anything hidden: a name starting with a dot, or inside one,
// stays here (.env, .git, .ssh).
func Files(request FilesRequest, machine *settings.Machine) map[string]any {
	answer := map[string]any{"type": "files_result", "request": request.ID}
	var err error
	switch request.Op {
	case "list":
		err = listFiles(request.Path, machine, answer)
	case "read":
		err = readFile(request.Path, machine, answer)
	default:
		err = fmt.Errorf("unknown files op %q", request.Op)
	}
	if err != nil {
		return map[string]any{"type": "files_result", "request": request.ID, "ok": false, "error": err.Error()}
	}
	answer["ok"] = true
	return answer
}

// filesTarget checks that path may be shown: files on, absolute, inside a root once links are
// resolved, and nothing hidden between that root and it. It returns the resolved path.
func filesTarget(path string, machine *settings.Machine) (string, error) {
	if !machine.Files {
		return "", errors.New("this machine doesn't share its files (agent-machine files on)")
	}
	if path == "" || !filepath.IsAbs(path) {
		return "", errors.New("name a path inside one of this machine's launch folders")
	}
	resolved, ok := machine.Within(path)
	if !ok {
		return "", fmt.Errorf("%s isn't inside one of this machine's launch folders", path)
	}
	if hiddenWithin(resolved, machine) || hiddenWithin(filepath.Clean(path), machine) {
		return "", fmt.Errorf("%s is hidden", path)
	}
	return resolved, nil
}

// hiddenWithin says whether any name below the root holding path starts with a dot.
func hiddenWithin(path string, machine *settings.Machine) bool {
	for _, root := range machine.Roots {
		for _, base := range []string{filepath.Clean(root), resolvedOr(root)} {
			relative, err := filepath.Rel(fold(base), fold(path))
			if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) || filepath.IsAbs(relative) {
				continue
			}
			for _, name := range strings.Split(relative, string(filepath.Separator)) {
				if strings.HasPrefix(name, ".") && name != "." {
					return true
				}
			}
		}
	}
	return false
}

func resolvedOr(path string) string {
	if resolved, err := filepath.EvalSymlinks(path); err == nil {
		return resolved
	}
	return filepath.Clean(path)
}

func listFiles(dir string, machine *settings.Machine, answer map[string]any) error {
	if _, err := filesTarget(dir, machine); err != nil {
		return err
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return fmt.Errorf("couldn't read %s: %v", dir, err)
	}
	listed := []map[string]any{}
	truncated := false
	for _, entry := range entries {
		name := entry.Name()
		if strings.HasPrefix(name, ".") {
			continue
		}
		path := filepath.Join(dir, name)
		info, err := os.Stat(path)
		if err != nil {
			continue
		}
		if entry.Type()&os.ModeSymlink != 0 {
			// A link is shown only when it leads somewhere inside the roots.
			if _, ok := machine.Within(path); !ok {
				continue
			}
		}
		kind := "file"
		if info.IsDir() {
			kind = "folder"
		} else if !info.Mode().IsRegular() {
			continue
		}
		if len(listed) == FilesLimit {
			truncated = true
			break
		}
		item := map[string]any{"name": name, "path": path, "kind": kind, "modified": info.ModTime().Unix()}
		if kind == "file" {
			item["size"] = info.Size()
		}
		listed = append(listed, item)
	}
	// Folders first, then files, each by name.
	sort.SliceStable(listed, func(i, j int) bool {
		if listed[i]["kind"] != listed[j]["kind"] {
			return listed[i]["kind"] == "folder"
		}
		return strings.ToLower(listed[i]["name"].(string)) < strings.ToLower(listed[j]["name"].(string))
	})
	// Up goes no further than a root.
	var parent any
	if !isRoot(dir, machine) {
		if up := filepath.Dir(dir); up != dir {
			if _, ok := machine.Within(up); ok {
				parent = up
			}
		}
	}
	answer["path"], answer["entries"], answer["parent"], answer["truncated"] = dir, listed, parent, truncated
	return nil
}

func readFile(path string, machine *settings.Machine, answer map[string]any) error {
	resolved, err := filesTarget(path, machine)
	if err != nil {
		return err
	}
	info, err := os.Stat(resolved)
	if err != nil {
		return fmt.Errorf("couldn't read %s: %v", path, err)
	}
	if !info.Mode().IsRegular() {
		return fmt.Errorf("%s isn't a file", path)
	}
	file, err := os.Open(resolved)
	if err != nil {
		return fmt.Errorf("couldn't read %s: %v", path, err)
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, FileReadLimit+1))
	if err != nil {
		return fmt.Errorf("couldn't read %s: %v", path, err)
	}
	truncated := len(data) > FileReadLimit
	if truncated {
		data = data[:FileReadLimit]
	}
	answer["path"], answer["name"], answer["size"], answer["modified"], answer["truncated"] = path, filepath.Base(path), info.Size(), info.ModTime().Unix(), truncated
	text, ok := textOf(data, truncated)
	if !ok {
		// Its bytes stay here: the viewer shows text only.
		answer["binary"] = true
		return nil
	}
	answer["binary"], answer["content"] = false, text
	return nil
}

// textOf is data as text, or false when it isn't: a NUL byte, or bytes that aren't UTF-8. A
// character cut off at the end of a truncated read is dropped rather than counted against it.
func textOf(data []byte, truncated bool) (string, bool) {
	if bytes.IndexByte(data, 0) >= 0 {
		return "", false
	}
	if truncated {
		for cut := 0; cut < utf8.UTFMax && len(data) > 0 && !utf8.Valid(data); cut++ {
			data = data[:len(data)-1]
		}
	}
	return string(data), utf8.Valid(data)
}

func fold(path string) string {
	if runtime.GOOS == "windows" {
		return strings.ToLower(path)
	}
	return path
}
