// Package settings holds what the agent machine's programs share: their ports, the
// server this machine signed in to (`agent login`), and HTTP clients that never use
// the environment's proxy.
//
// Nothing here reads the server's config/ files: the client only knows the server
// by its URL, and asks it (through the agent-proxy) for everything else.
package settings

import (
	"encoding/json"
	"fmt"
	"net"
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
	// Where the agent-proxy listens, loopback only: its launcher API and agents' model
	// endpoints, and the HTTPS proxy account agents use.
	DefaultProxyPort     = 8184
	DefaultInterceptPort = 8183
	// The server before `agent login` names one: an agent-server on this computer.
	DefaultServer = "http://127.0.0.1:9999"
	// The header the server reads a launcher token (or a server's access key) from.
	TokenHeader = "X-AI-Proxy-Token"
)

// Version is the client's version, set by the binaries from their build (-ldflags -X).
var Version = "dev"

// Protocol is the server API level this client speaks. The server accepts a range of
// them and refuses others with 426; raise it only for a change older servers can't take.
// The server's and agent-machine's must agree (server/tests/test_client.py checks).
const Protocol = 1

// Program is how this binary names itself to the server, in its User-Agent.
var Program = "agent"

// The headers every request to the server carries, so it can refuse a client it can't serve.
const (
	VersionHeader  = "X-AI-Proxy-Version"
	ProtocolHeader = "X-AI-Proxy-Protocol"
)

// Identify adds this client's version and protocol to headers bound for the server, and
// its User-Agent when agent is set (forwarded agent traffic keeps the agent's own).
func Identify(header http.Header, agent bool) {
	header.Set(VersionHeader, Version)
	header.Set(ProtocolHeader, strconv.Itoa(Protocol))
	if agent {
		header.Set("User-Agent", Program+"/"+Version)
	}
}

// identified adds Identify's headers to every request it carries.
type identified struct {
	base      http.RoundTripper
	userAgent bool
}

func (t identified) RoundTrip(request *http.Request) (*http.Response, error) {
	request = request.Clone(request.Context())
	Identify(request.Header, t.userAgent)
	return t.base.RoundTrip(request)
}

// ServerTransport is Transport with this client's version and protocol on every request.
// userAgent replaces the User-Agent too; leave it off for traffic forwarded for an agent.
func ServerTransport(userAgent bool) http.RoundTripper {
	return identified{base: Transport(), userAgent: userAgent}
}

// ServerClient is Client for requests to the server, identified (ServerTransport).
func ServerClient(timeout time.Duration) *http.Client {
	return &http.Client{Transport: ServerTransport(true), Timeout: timeout}
}

// Platform is this build's os-arch as dist names it: 32-bit ARM is armv7.
func Platform() string {
	arch := runtime.GOARCH
	if arch == "arm" {
		arch = "armv7"
	}
	return runtime.GOOS + "-" + arch
}

// Loopback names the programs here accept in a Host header.
var Loopback = map[string]bool{"localhost": true, "127.0.0.1": true, "::1": true}

func ProxyPort() int { return envPort("AI_PROXY_LOCAL_PORT", DefaultProxyPort) }

func InterceptPort() int { return envPort("AI_PROXY_LOCAL_INTERCEPT_PORT", DefaultInterceptPort) }

func ProxyURL() string { return fmt.Sprintf("http://127.0.0.1:%d", ProxyPort()) }

func envPort(name string, fallback int) int {
	if value, err := strconv.Atoi(os.Getenv(name)); err == nil && value > 0 {
		return value
	}
	return fallback
}

// Transport never takes a proxy from the environment: an HTTPS_PROXY inherited from an
// account agent points at the agent-proxy itself, and would loop.
func Transport() *http.Transport {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	return transport
}

// Client is an HTTP client without an environment proxy; timeout 0 means none.
func Client(timeout time.Duration) *http.Client {
	return &http.Client{Transport: Transport(), Timeout: timeout}
}

// IsLoopbackURL tells whether a URL names this computer.
func IsLoopbackURL(raw string) bool {
	parsed, err := url.Parse(raw)
	return err == nil && Loopback[strings.ToLower(parsed.Hostname())]
}

// LoopbackHost tells whether a Host header names this computer, with or without a port.
func LoopbackHost(host string) bool {
	host = strings.ToLower(strings.TrimSpace(host))
	if name, _, err := net.SplitHostPort(host); err == nil {
		host = name
	}
	return Loopback[strings.Trim(host, "[]")]
}

// Credentials is `agent login`'s file: each server's URL, user and launcher token, and
// the default server. The password is never stored. The format is shared with earlier
// (Python) versions of the client, so existing sign-ins carry over.
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

// CredentialsPath is credentials.json in the user's config folder, or AI_PROXY_CREDENTIALS.
func CredentialsPath() string {
	if path := os.Getenv("AI_PROXY_CREDENTIALS"); path != "" {
		return expandHome(path)
	}
	if runtime.GOOS == "windows" {
		base := os.Getenv("APPDATA")
		if base == "" {
			home, _ := os.UserHomeDir()
			base = filepath.Join(home, "AppData", "Roaming")
		}
		return filepath.Join(base, "agent-proxy", "credentials.json")
	}
	base := os.Getenv("XDG_CONFIG_HOME")
	if base == "" {
		home, _ := os.UserHomeDir()
		base = filepath.Join(home, ".config")
	}
	return filepath.Join(base, "agent-proxy", "credentials.json")
}

func expandHome(path string) string {
	if strings.HasPrefix(path, "~") {
		if home, err := os.UserHomeDir(); err == nil {
			return filepath.Join(home, path[1:])
		}
	}
	return path
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

// Save writes the file readable by its owner only, replacing it in one step.
func (c *Credentials) Save() error {
	path := CredentialsPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	data, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return err
	}
	temporary := path + ".tmp"
	if err := os.WriteFile(temporary, append(data, '\n'), 0o600); err != nil {
		return err
	}
	return os.Rename(temporary, path)
}

// DefaultEntry is the server signed in to last, if it's still stored.
func (c *Credentials) DefaultEntry() *ServerEntry {
	if c.Default == "" {
		return nil
	}
	return c.Servers[c.Default]
}

// Upstream is the agent-server this machine uses and its sign-in. It's read from the
// credentials file on every call, so `agent login` and `agent logout` apply at once.
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

// DefaultServerURL is AI_PROXY_URL, then the server from `agent login`, then this computer.
func DefaultServerURL() string {
	if server := os.Getenv("AI_PROXY_URL"); server != "" {
		return strings.TrimRight(server, "/")
	}
	if entry := LoadCredentials().DefaultEntry(); entry != nil && entry.URL != "" {
		return strings.TrimRight(entry.URL, "/")
	}
	return DefaultServer
}

// Token is AI_PROXY_TOKEN, or the stored token for server: a launcher token on a server
// with accounts, or (with `agent login --key`) the access key of one without.
func (u Upstream) Token(server string) string {
	if token := os.Getenv("AI_PROXY_TOKEN"); token != "" {
		return token
	}
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
