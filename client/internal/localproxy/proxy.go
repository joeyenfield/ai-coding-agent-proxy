// Package localproxy is agent-proxy: the proxy on each agent machine, which the agents
// there register with.
//
// It listens on this machine's loopback only and carries everything to the agent-server
// this machine signed in to with `agent login <server>`:
//
//   - Its HTTP port (8184) takes the launcher's API calls (sessions, the server's CA, ...),
//     signed with the machine's sign-in, and agents' model endpoints (/session/<id>/...),
//     streamed through unchanged. Terminals shared with `agent --control` are relayed too.
//   - Its HTTPS proxy port (8183) is where account agents' HTTPS_PROXY points. Each
//     connection is tunnelled to the server over a WebSocket, and the server decrypts,
//     records and forwards it.
//
// So agents only ever talk to this machine, and the server needs nothing but its own port.
package localproxy

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"regexp"
	"strconv"
	"strings"

	"ai-coding-agent-proxy/client/internal/settings"
)

type route struct {
	method string
	path   *regexp.Regexp
}

// LauncherAPI is the server API the launcher uses, and nothing more: any program on this
// machine can reach the proxy, and what it forwards here is signed as you.
var LauncherAPI = []route{
	{"GET", regexp.MustCompile(`^/api/auth/status$`)},
	{"GET", regexp.MustCompile(`^/api/config$`)},
	{"GET", regexp.MustCompile(`^/api/agents$`)},
	{"POST", regexp.MustCompile(`^/api/agents/[^/]+/sessions$`)},
	{"GET", regexp.MustCompile(`^/api/intercept(/ca\.pem|/bundle\.pem)?$`)},
	{"GET", regexp.MustCompile(`^/api/remote$`)},
	{"POST", regexp.MustCompile(`^/api/sessions$`)},
	{"PATCH", regexp.MustCompile(`^/api/sessions/[^/]+$`)},
	{"GET", regexp.MustCompile(`^/api/containers/[^/]+$`)},
	{"POST", regexp.MustCompile(`^/api/containers/[^/]+/start$`)},
}

var terminalHost = regexp.MustCompile(`^/api/terminals/([^/]+)/host$`)

// Not passed on: hop-by-hop headers, and the browser's own credentials and origin (the
// proxy signs instead).
var skippedHeaders = map[string]bool{
	"host": true, "connection": true, "keep-alive": true, "transfer-encoding": true, "te": true, "trailer": true,
	"upgrade": true, "content-length": true, "proxy-authorization": true, "proxy-connection": true,
	"cookie": true, "origin": true,
}

// Proxy is the agent-proxy's HTTP side. InterceptPort is the HTTPS proxy's actual port,
// reported in /health and handed to launchers in /api/intercept.
type Proxy struct {
	Upstream      settings.Upstream
	InterceptPort int
	Logger        *log.Logger
	// Shutdown ends the proxy, for POST /shutdown (agent proxy stop, agent update); nil refuses it.
	Shutdown func()
	client   *http.Client
	agents   *httputil.ReverseProxy
}

func New(upstream settings.Upstream, interceptPort int, logger *log.Logger) *Proxy {
	p := &Proxy{Upstream: upstream, InterceptPort: interceptPort, Logger: logger, client: &http.Client{Transport: settings.ServerTransport(false)}}
	p.agents = &httputil.ReverseProxy{
		Rewrite: p.rewriteAgent,
		// Identified, so the server can refuse a client too old for it; the agent keeps its own User-Agent.
		Transport: settings.ServerTransport(false),
		// Stream every chunk at once: agents read server-sent events as they arrive.
		FlushInterval: -1,
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			message := fmt.Sprintf("agent-proxy couldn't reach the agent-server at %s: %v", p.Upstream.Server(), err)
			writeJSON(w, http.StatusBadGateway, map[string]any{"error": map[string]any{"message": message, "type": "proxy_error"}})
		},
	}
	return p
}

func (p *Proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Path
	switch {
	case path == "/health" && r.Method == http.MethodGet:
		server := p.Upstream.Server()
		writeJSON(w, http.StatusOK, map[string]any{
			"status": "ok", "role": "agent-proxy", "server": server, "user": nullable(p.Upstream.User(server)),
			"intercept_port": p.InterceptPort, "version": settings.Version, "protocol": settings.Protocol,
		})
	case path == "/shutdown":
		p.shutdown(w, r)
	case terminalHost.MatchString(path):
		p.terminal(w, r, terminalHost.FindStringSubmatch(path)[1])
	case strings.HasPrefix(path, "/api/"):
		p.launcherAPI(w, r)
	case strings.HasPrefix(path, "/session/"), strings.HasPrefix(path, "/v1/"), strings.HasPrefix(path, "/anthropic/"):
		if !local(r) {
			refuse(w)
			return
		}
		// The session id in the path (or X-AI-Proxy-Session) is the credential: no token.
		p.agents.ServeHTTP(w, r)
	default:
		writeJSON(w, http.StatusNotFound, map[string]any{"detail": "Not found"})
	}
}

// launcherAPI forwards the launcher's calls to the server, signed with this machine's sign-in.
func (p *Proxy) launcherAPI(w http.ResponseWriter, r *http.Request) {
	if !local(r) {
		refuse(w)
		return
	}
	if !allowed(r.Method, r.URL.Path) {
		writeJSON(w, http.StatusNotFound, map[string]any{"detail": "Not found"})
		return
	}
	server := p.Upstream.Server()
	body, err := io.ReadAll(r.Body)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"detail": err.Error()})
		return
	}
	request, err := http.NewRequestWithContext(r.Context(), r.Method, server+r.URL.RequestURI(), bytes.NewReader(body))
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]any{"detail": err.Error()})
		return
	}
	copyHeaders(request.Header, r.Header)
	// Uncompressed, so /api/intercept can be read and rewritten below.
	request.Header.Del("Accept-Encoding")
	if token := p.Upstream.Token(server); token != "" {
		request.Header.Set(settings.TokenHeader, token)
	}
	response, err := p.client.Do(request)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]any{"detail": fmt.Sprintf("agent-proxy couldn't reach the agent-server at %s: %v", server, err)})
		return
	}
	defer response.Body.Close()
	answer, err := io.ReadAll(response.Body)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]any{"detail": err.Error()})
		return
	}
	if r.URL.Path == "/api/intercept" && response.StatusCode == http.StatusOK {
		// Agents on this machine reach the intercept through this proxy's own port.
		var info map[string]any
		if json.Unmarshal(answer, &info) == nil {
			info["url"] = fmt.Sprintf("http://127.0.0.1:%d", p.InterceptPort)
			writeJSON(w, http.StatusOK, info)
			return
		}
	}
	copyHeaders(w.Header(), response.Header)
	w.Header().Set("Content-Length", strconv.Itoa(len(answer)))
	w.WriteHeader(response.StatusCode)
	_, _ = w.Write(answer)
}

// rewriteAgent sends an agent's request to the same path on the server, unchanged apart
// from the headers that belong to this hop or to a browser.
func (p *Proxy) rewriteAgent(r *httputil.ProxyRequest) {
	target, err := url.Parse(p.Upstream.Server() + r.In.URL.RequestURI())
	if err != nil {
		target = &url.URL{Scheme: "http", Host: "invalid", Path: "/"}
	}
	r.Out.URL = target
	r.Out.Host = ""
	for _, name := range []string{"Cookie", "Origin", "Proxy-Authorization", "Proxy-Connection"} {
		r.Out.Header.Del(name)
	}
}

// shutdown ends the proxy when a program on this computer asks: `agent proxy stop`, and
// `agent update` so the next `agent` starts the new binary.
func (p *Proxy) shutdown(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]any{"detail": "POST to stop agent-proxy"})
		return
	}
	// A cross-site page can POST without preflight; Origin and Host keep it out like everything else.
	if !local(r) {
		refuse(w)
		return
	}
	if p.Shutdown == nil {
		writeJSON(w, http.StatusNotFound, map[string]any{"detail": "Not found"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"stopping": true, "version": settings.Version})
	if flusher, ok := w.(http.Flusher); ok {
		flusher.Flush()
	}
	go p.Shutdown()
}

func allowed(method, path string) bool {
	for _, candidate := range LauncherAPI {
		if candidate.method == method && candidate.path.MatchString(path) {
			return true
		}
	}
	return false
}

// local: a program on this computer naming it in Host, not a web page (DNS rebinding, or
// another site's page).
func local(r *http.Request) bool {
	return settings.LoopbackHost(r.Host) && sameOrigin(r)
}

// sameOrigin: browsers send Origin with WebSockets and cross-site requests; programs don't.
func sameOrigin(r *http.Request) bool {
	origin := r.Header.Get("Origin")
	if origin == "" {
		return true
	}
	parsed, err := url.Parse(origin)
	return err == nil && strings.EqualFold(parsed.Host, r.Host)
}

func refuse(w http.ResponseWriter) {
	writeJSON(w, http.StatusForbidden, map[string]any{"detail": "agent-proxy only serves programs on this computer."})
}

func copyHeaders(to, from http.Header) {
	for name, values := range from {
		if skippedHeaders[strings.ToLower(name)] {
			continue
		}
		for _, value := range values {
			to.Add(name, value)
		}
	}
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	data, _ := json.Marshal(body)
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Length", strconv.Itoa(len(data)))
	w.WriteHeader(status)
	_, _ = w.Write(data)
}

func nullable(value string) any {
	if value == "" {
		return nil
	}
	return value
}

// Listen binds a loopback port; 0 picks a free one.
func Listen(port int) (net.Listener, error) {
	return net.Listen("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)))
}
