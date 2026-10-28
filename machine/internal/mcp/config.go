// Package mcp shares MCP (Model Context Protocol) servers on this machine with the server's
// agents: the ones its owner lists with `agent-machine mcp add` (mcp.json in agent-machine's
// folder), and nothing else.
//
// Every one of them is reached the same way, through the bridge: an HTTP server on a
// loopback port that speaks MCP's Streamable HTTP transport, at /mcp/<name>, behind a key
// that only this run of agent-machine and the server know. The server reaches that port
// through the fleet's tunnel (presence/tunnel.go), like a model server. A stdio server
// (a command, such as npx @modelcontextprotocol/server-github) gets a process of its own for
// each MCP session; an HTTP one (a URL) is proxied. Its command, environment, folder and
// headers stay here: the server only ever learns the names.
package mcp

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"

	"ai-coding-agent-proxy/machine/internal/process"
	"ai-coding-agent-proxy/machine/internal/settings"
)

// Server is one MCP server this machine shares: a command run over stdio, or a URL.
type Server struct {
	Name        string            `json:"name"`
	Description string            `json:"description,omitempty"`
	Command     []string          `json:"command,omitempty"`
	Env         map[string]string `json:"env,omitempty"`
	Dir         string            `json:"dir,omitempty"`
	URL         string            `json:"url,omitempty"`
	// Headers go with every request to a URL server (an Authorization header, say).
	Headers map[string]string `json:"headers,omitempty"`
}

// Transport is "stdio" for a command and "http" for a URL.
func (s Server) Transport() string {
	if s.URL != "" {
		return "http"
	}
	return "stdio"
}

var name = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,39}$`)

// Check says what's wrong with a server's settings, if anything.
func (s Server) Check() error {
	if !name.MatchString(s.Name) {
		return fmt.Errorf("name an MCP server with up to 40 lowercase letters, numbers, '-' and '_' (not %q)", s.Name)
	}
	if (s.URL == "") == (len(s.Command) == 0) {
		return errors.New("give an MCP server either a command (after --) or --url")
	}
	if s.URL != "" {
		parsed, err := url.Parse(s.URL)
		if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" {
			return fmt.Errorf("%s isn't an http:// or https:// URL", s.URL)
		}
	}
	return nil
}

// Found says whether a stdio server's command is installed here; URL servers always are.
func (s Server) Found() bool {
	if s.URL != "" {
		return true
	}
	_, err := process.LookPath(s.Command[0])
	return err == nil
}

// Path is mcp.json in agent-machine's folder.
func Path() string { return filepath.Join(settings.Home(), "mcp.json") }

type file struct {
	Servers []Server `json:"servers"`
}

// Load reads the servers in mcp.json, sorted by name; none when there's no file.
func Load() ([]Server, error) {
	data, err := os.ReadFile(Path())
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var parsed file
	if err := json.Unmarshal(data, &parsed); err != nil {
		return nil, fmt.Errorf("%s: %v", Path(), err)
	}
	valid := parsed.Servers[:0]
	for _, server := range parsed.Servers {
		if server.Check() == nil {
			valid = append(valid, server)
		}
	}
	sort.Slice(valid, func(i, j int) bool { return valid[i].Name < valid[j].Name })
	return valid, nil
}

// Save writes mcp.json, readable by this user only: environments and headers often hold tokens.
func Save(servers []Server) error {
	if err := os.MkdirAll(settings.Home(), 0o700); err != nil {
		return err
	}
	data, err := json.MarshalIndent(file{Servers: servers}, "", "  ")
	if err != nil {
		return err
	}
	temporary := Path() + ".tmp"
	if err := os.WriteFile(temporary, append(data, '\n'), 0o600); err != nil {
		return err
	}
	return os.Rename(temporary, Path())
}

// Add saves a server, replacing one of the same name.
func Add(server Server) error {
	if err := server.Check(); err != nil {
		return err
	}
	servers, err := Load()
	if err != nil {
		return err
	}
	kept := []Server{}
	for _, existing := range servers {
		if existing.Name != server.Name {
			kept = append(kept, existing)
		}
	}
	return Save(append(kept, server))
}

// Remove takes a server out; false when there was none of that name.
func Remove(serverName string) (bool, error) {
	servers, err := Load()
	if err != nil {
		return false, err
	}
	kept := []Server{}
	for _, existing := range servers {
		if existing.Name != serverName {
			kept = append(kept, existing)
		}
	}
	if len(kept) == len(servers) {
		return false, nil
	}
	return true, Save(kept)
}
