package presence

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"

	"ai-coding-agent-proxy/machine/internal/settings"
)

// FoldersLimit is the most folders one listing returns; a folder with more says it's truncated.
const FoldersLimit = 500

// FoldersRequest is the server's {"type": "folders"}: the folders inside one folder, for the
// console's folder picker when starting an agent or a shell here.
type FoldersRequest struct {
	Type string `json:"type"`
	ID   string `json:"request"`
	Op   string `json:"op"`
	Path string `json:"path"`
}

// Folders answers a folders request, as the folders_result to send back. Only folders inside the
// roots are listed (checked here after resolving links, whatever the server allowed), only while
// launches or commands are on, and only folders: no file names leave the machine. Hidden folders
// and links that lead out of the roots are left out.
func Folders(request FoldersRequest, machine *settings.Machine) map[string]any {
	answer := map[string]any{"type": "folders_result", "request": request.ID}
	folders, parent, truncated, err := listFolders(request.Path, machine)
	if err != nil {
		answer["ok"], answer["error"] = false, err.Error()
		return answer
	}
	answer["ok"], answer["folders"], answer["parent"], answer["truncated"] = true, folders, parent, truncated
	return answer
}

func listFolders(dir string, machine *settings.Machine) ([]map[string]string, any, bool, error) {
	if !machine.Launches && !machine.Commands {
		return nil, nil, false, errors.New("this machine doesn't take launches (agent-machine launches on)")
	}
	if dir == "" || !filepath.IsAbs(dir) {
		return nil, nil, false, errors.New("name a folder inside one of this machine's launch folders")
	}
	if _, ok := machine.Within(dir); !ok {
		return nil, nil, false, fmt.Errorf("%s isn't inside one of this machine's launch folders", dir)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, nil, false, fmt.Errorf("couldn't read %s: %v", dir, err)
	}
	folders := []map[string]string{}
	truncated := false
	for _, entry := range entries {
		name := entry.Name()
		if strings.HasPrefix(name, ".") {
			continue
		}
		path := filepath.Join(dir, name)
		if entry.Type()&os.ModeSymlink != 0 {
			// A link counts as a folder only when it leads to one inside the roots.
			if info, err := os.Stat(path); err != nil || !info.IsDir() {
				continue
			}
			if _, ok := machine.Within(path); !ok {
				continue
			}
		} else if !entry.IsDir() {
			continue
		}
		if len(folders) == FoldersLimit {
			truncated = true
			break
		}
		folders = append(folders, map[string]string{"name": name, "path": path})
	}
	sort.Slice(folders, func(i, j int) bool { return strings.ToLower(folders[i]["name"]) < strings.ToLower(folders[j]["name"]) })
	// Up goes no further than a root.
	var parent any
	if !isRoot(dir, machine) {
		if up := filepath.Dir(dir); up != dir {
			if _, ok := machine.Within(up); ok {
				parent = up
			}
		}
	}
	return folders, parent, truncated, nil
}

func isRoot(dir string, machine *settings.Machine) bool {
	for _, root := range machine.Roots {
		a, b := filepath.Clean(root), filepath.Clean(dir)
		if a == b || (runtime.GOOS == "windows" && strings.EqualFold(a, b)) {
			return true
		}
	}
	return false
}
