package settings

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

// Machine is this machine as the server's fleet knows it (machine.json in Home): a
// stable id, and what the server may do here: start agents (in which folders) and run
// containers. Both are off until the owner turns them on.
type Machine struct {
	ID       string   `json:"id"`
	Name     string   `json:"name,omitempty"`
	Launches bool     `json:"launches"`
	Roots    []string `json:"roots,omitempty"`
	// Address is how the server reaches this machine's model servers, when the address it
	// sees (behind NAT or a reverse proxy) isn't it.
	Address string `json:"address,omitempty"`
	// Containers lets the server run model servers here in containers. `docker run` is
	// root on this machine, so it's off until the owner turns it on.
	Containers bool `json:"containers"`
	// ContainerCLI is "docker" or "podman"; empty (the default) uses whichever is installed and answering.
	ContainerCLI string `json:"container_cli,omitempty"`
	// Servers lets the server start and stop the Ollama and Strata installed here. Off
	// until the owner turns it on, like containers.
	Servers bool `json:"servers"`
	// Commands lets the owner open a shell here from the console, in one of the roots: a
	// remote terminal, so it's off until the owner turns it on.
	Commands bool `json:"commands"`
}

func MachinePath() string {
	return filepath.Join(Home(), "machine.json")
}

// formerPath is where the agent command kept machine.json before agent-machine existed.
func formerPath() string {
	return filepath.Join(configDir(), "agent-proxy", "machine.json")
}

// LoadMachine reads machine.json, creating the id on first use.
func LoadMachine() *Machine {
	machine := &Machine{}
	data, err := os.ReadFile(MachinePath())
	if err != nil && os.Getenv("AI_PROXY_MACHINE_HOME") == "" {
		// Carry the old file over once: the server knows the machine (and its backends) by its id.
		if data, err = os.ReadFile(formerPath()); err == nil {
			_ = os.MkdirAll(Home(), 0o700)
			_ = os.WriteFile(MachinePath(), data, 0o600)
		}
	}
	if err == nil {
		_ = json.Unmarshal(data, machine)
	}
	if machine.ID == "" {
		buffer := make([]byte, 8)
		_, _ = rand.Read(buffer)
		machine.ID = hex.EncodeToString(buffer)
		_ = machine.Save()
	}
	return machine
}

func (m *Machine) Save() error {
	return writeJSON(MachinePath(), m)
}

// DisplayName is the name the console shows: the one set with `agent-machine name`, or the hostname.
func (m *Machine) DisplayName() string {
	if m.Name != "" {
		return m.Name
	}
	name, err := os.Hostname()
	if err != nil || name == "" {
		return m.ID
	}
	return name
}

// Within tells whether dir is one of the roots or inside one. Both are made absolute and
// symlinks resolved, so "..", links and case on Windows can't step outside.
func (m *Machine) Within(dir string) (string, bool) {
	resolved, err := realPath(dir)
	if err != nil {
		return "", false
	}
	for _, root := range m.Roots {
		base, err := realPath(root)
		if err != nil {
			continue
		}
		relative, err := filepath.Rel(fold(base), fold(resolved))
		if err == nil && relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator)) && !filepath.IsAbs(relative) {
			return resolved, true
		}
	}
	return "", false
}

func realPath(path string) (string, error) {
	absolute, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	return filepath.EvalSymlinks(absolute)
}

func fold(path string) string {
	if runtime.GOOS == "windows" {
		return strings.ToLower(path)
	}
	return path
}
