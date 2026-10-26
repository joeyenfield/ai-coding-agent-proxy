// Package settings holds agent-machine's own state, in its own folder (Home): the server
// it signed in to with `agent-machine login` (credentials.json), the machine's settings
// (machine.json), its log and how its connection is doing (status.json).
//
// It's separate from the agent command's sign-in on purpose: a machine can supply models
// to one server, or as one account, while its agents use another. Nothing here reads the
// server's config/ files or the client's: the machine knows the server only by its URL.
package settings

import (
	"encoding/json"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"
)

const (
	// The server before `agent-machine login` names one: an agent-server on this computer.
	DefaultServer = "http://127.0.0.1:9999"
	// The header the server reads a sign-in token (or a server's access key) from.
	TokenHeader = "X-AI-Proxy-Token"
)

// Version is agent-machine's version, set from its build (-ldflags -X).
var Version = "dev"

// Protocol is the server API level agent-machine speaks. The server accepts a range of
// them and refuses others with 426; raise it only for a change older servers can't take.
// The server's and the client's must agree (server/tests/test_client.py checks).
const Protocol = 1

// The headers every request to the server carries, so it can refuse a machine it can't serve.
const (
	VersionHeader  = "X-AI-Proxy-Version"
	ProtocolHeader = "X-AI-Proxy-Protocol"
)

// Identify adds agent-machine's version, protocol and User-Agent to headers bound for the server.
func Identify(header http.Header) {
	header.Set(VersionHeader, Version)
	header.Set(ProtocolHeader, strconv.Itoa(Protocol))
	header.Set("User-Agent", "agent-machine/"+Version)
}

// identified adds Identify's headers to every request it carries.
type identified struct{ base http.RoundTripper }

func (t identified) RoundTrip(request *http.Request) (*http.Response, error) {
	request = request.Clone(request.Context())
	Identify(request.Header)
	return t.base.RoundTrip(request)
}

// ServerClient is Client for requests to the agent-server, with Identify's headers. Model
// servers and model catalogues get the plain Client: they're not the server's business.
func ServerClient(timeout time.Duration) *http.Client {
	return &http.Client{Transport: identified{base: Transport()}, Timeout: timeout}
}

// Platform is this build's os-arch as dist names it: 32-bit ARM is armv7.
func Platform() string {
	arch := runtime.GOARCH
	if arch == "arm" {
		arch = "armv7"
	}
	return runtime.GOOS + "-" + arch
}

// Home is agent-machine's folder: AI_PROXY_MACHINE_HOME, or agent-machine in the user's config folder.
func Home() string {
	if path := os.Getenv("AI_PROXY_MACHINE_HOME"); path != "" {
		return expandHome(path)
	}
	return filepath.Join(configDir(), "agent-machine")
}

// configDir is the user's config folder: %APPDATA% on Windows, XDG_CONFIG_HOME or ~/.config elsewhere.
func configDir() string {
	if runtime.GOOS == "windows" {
		if base := os.Getenv("APPDATA"); base != "" {
			return base
		}
		home, _ := os.UserHomeDir()
		return filepath.Join(home, "AppData", "Roaming")
	}
	if base := os.Getenv("XDG_CONFIG_HOME"); base != "" {
		return base
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".config")
}

func CredentialsPath() string { return filepath.Join(Home(), "credentials.json") }

func LogPath() string { return filepath.Join(Home(), "agent-machine.log") }

func expandHome(path string) string {
	if strings.HasPrefix(path, "~") {
		if home, err := os.UserHomeDir(); err == nil {
			return filepath.Join(home, path[1:])
		}
	}
	return path
}

// Transport never takes a proxy from the environment: started from inside an
// intercepted agent, it would otherwise inherit an HTTPS_PROXY meant for that agent.
func Transport() *http.Transport {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	return transport
}

// Client is an HTTP client without an environment proxy; timeout 0 means none.
func Client(timeout time.Duration) *http.Client {
	return &http.Client{Transport: Transport(), Timeout: timeout}
}

var loopback = map[string]bool{"localhost": true, "127.0.0.1": true, "::1": true}

// IsLoopbackURL tells whether a URL names this computer.
func IsLoopbackURL(raw string) bool {
	parsed, err := url.Parse(raw)
	return err == nil && loopback[strings.ToLower(parsed.Hostname())]
}

// Credentials is `agent-machine login`'s file: each server's URL, user and token, and the
// default server. The password is never stored.
type Credentials struct {
	Servers map[string]*ServerEntry `json:"servers"`
	Default string                  `json:"default,omitempty"`
}

type ServerEntry struct {
	URL     string `json:"url"`
	User    string `json:"user,omitempty"`
	Token   string `json:"token,omitempty"`
	TokenID string `json:"token_id,omitempty"`
}

// Normalize is the key a server is stored under.
func Normalize(server string) string {
	return strings.ToLower(strings.TrimRight(strings.TrimSpace(server), "/"))
}

// LoadCredentials reads the file; a missing or unreadable one is empty.
func LoadCredentials() *Credentials {
	credentials := &Credentials{}
	if data, err := os.ReadFile(CredentialsPath()); err == nil {
		_ = json.Unmarshal(data, credentials)
	}
	if credentials.Servers == nil {
		credentials.Servers = map[string]*ServerEntry{}
	}
	return credentials
}

func (c *Credentials) Save() error {
	return writeJSON(CredentialsPath(), c)
}

// DefaultEntry is the server signed in to last, if it's still stored.
func (c *Credentials) DefaultEntry() *ServerEntry {
	if c.Default == "" {
		return nil
	}
	return c.Servers[c.Default]
}

// Upstream is the agent-server this machine supplies and its sign-in, read from the
// credentials file on every call, so `agent-machine login` and `logout` apply at once.
type Upstream struct {
	// Fixed is the server from --server; it wins over everything else.
	Fixed string
}

func (u Upstream) Server() string {
	if u.Fixed != "" {
		return strings.TrimRight(u.Fixed, "/")
	}
	return DefaultServerURL()
}

// DefaultServerURL is the server from `agent-machine login`, or this computer.
func DefaultServerURL() string {
	if entry := LoadCredentials().DefaultEntry(); entry != nil && entry.URL != "" {
		return strings.TrimRight(entry.URL, "/")
	}
	return DefaultServer
}

// Token is the stored token for server: a sign-in token on a server with accounts, or
// (with `agent-machine login --key`) the access key of one without.
func (u Upstream) Token(server string) string {
	if entry := LoadCredentials().Servers[Normalize(server)]; entry != nil {
		return entry.Token
	}
	return ""
}

func (u Upstream) User(server string) string {
	if entry := LoadCredentials().Servers[Normalize(server)]; entry != nil {
		return entry.User
	}
	return ""
}

// Status is how a running `agent-machine run` is doing (status.json), for `agent-machine`
// and `agent-machine stop` in another terminal.
type Status struct {
	PID    int     `json:"pid"`
	Server string  `json:"server"`
	State  string  `json:"state"`
	At     float64 `json:"at"`
	// ServerVersion is the server's version when it last connected.
	ServerVersion string `json:"server_version,omitempty"`
	// Started is how this run began: "background" (agent-machine start), "service" (run with
	// no terminal: systemd, launchd, rc.d) or "terminal" (agent-machine run by hand).
	Started string `json:"started,omitempty"`
}

func StatusPath() string { return filepath.Join(Home(), "status.json") }

func LoadStatus() *Status {
	status := &Status{}
	if data, err := os.ReadFile(StatusPath()); err == nil {
		_ = json.Unmarshal(data, status)
	}
	return status
}

func (s *Status) Save() error {
	s.At = float64(time.Now().UnixNano()) / 1e9
	return writeJSON(StatusPath(), s)
}

// writeJSON replaces path in one step, readable by this user only.
func writeJSON(path string, value any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	temporary := path + ".tmp"
	if err := os.WriteFile(temporary, append(data, '\n'), 0o600); err != nil {
		return err
	}
	return os.Rename(temporary, path)
}

// Detail is the server's error message from a JSON body ({"detail": ...}), or the body itself.
func Detail(body []byte) string {
	var parsed struct {
		Detail any `json:"detail"`
	}
	if json.Unmarshal(body, &parsed) == nil && parsed.Detail != nil {
		if text, ok := parsed.Detail.(string); ok {
			return text
		}
		encoded, _ := json.Marshal(parsed.Detail)
		return string(encoded)
	}
	text := strings.TrimSpace(string(body))
	if text == "" {
		return "no details"
	}
	return text
}
