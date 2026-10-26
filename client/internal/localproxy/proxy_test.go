package localproxy

import (
	"bufio"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"

	"ai-coding-agent-proxy/client/internal/settings"
)

// fakeServer plays the agent-server and records what reached it.
type fakeServer struct {
	*httptest.Server
	mu   sync.Mutex
	seen []*http.Request
}

func (f *fakeServer) last() *http.Request {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.seen[len(f.seen)-1]
}

func newFakeServer(t *testing.T, handler http.HandlerFunc) *fakeServer {
	f := &fakeServer{}
	f.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		f.seen = append(f.seen, r.Clone(r.Context()))
		f.mu.Unlock()
		handler(w, r)
	}))
	t.Cleanup(f.Close)
	return f
}

// newProxy is an agent-proxy for server, as a test server whose URL names 127.0.0.1.
func newProxy(t *testing.T, server string) (*Proxy, *httptest.Server) {
	t.Setenv("AI_PROXY_CREDENTIALS", t.TempDir()+"/credentials.json")
	t.Setenv("AI_PROXY_TOKEN", "aip_machine")
	t.Setenv("AI_PROXY_URL", "")
	proxy := New(settings.Upstream{Fixed: server}, 18183, nil)
	front := httptest.NewServer(proxy)
	t.Cleanup(front.Close)
	return proxy, front
}

func do(t *testing.T, method, url string, headers map[string]string, body string) (*http.Response, string) {
	t.Helper()
	request, _ := http.NewRequest(method, url, strings.NewReader(body))
	for name, value := range headers {
		if strings.EqualFold(name, "host") {
			request.Host = value
		} else {
			request.Header.Set(name, value)
		}
	}
	response, err := settings.Client(5 * time.Second).Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	data, _ := io.ReadAll(response.Body)
	return response, string(data)
}

func TestLauncherCallsAreSignedAndLimitedToTheShortList(t *testing.T) {
	server := newFakeServer(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/intercept" {
			_ = json.NewEncoder(w).Encode(map[string]any{"running": true, "url": "http://127.0.0.1:8183", "ca_fingerprint": "AB:CD"})
			return
		}
		w.WriteHeader(http.StatusCreated)
		_, _ = io.WriteString(w, `{"session_id":"s1"}`)
	})
	_, front := newProxy(t, server.URL)

	response, body := do(t, "POST", front.URL+"/api/sessions", map[string]string{"Cookie": "ai_proxy_session=browser"}, `{"client":"qwen"}`)
	if response.StatusCode != http.StatusCreated || !strings.Contains(body, "s1") {
		t.Fatalf("POST /api/sessions: %d %s", response.StatusCode, body)
	}
	seen := server.last()
	if seen.Header.Get(settings.TokenHeader) != "aip_machine" || seen.Header.Get("Cookie") != "" {
		t.Fatalf("the server got token %q and cookie %q", seen.Header.Get(settings.TokenHeader), seen.Header.Get("Cookie"))
	}
	// The intercept's address is this proxy's own HTTPS port.
	_, body = do(t, "GET", front.URL+"/api/intercept", nil, "")
	if !strings.Contains(body, `"url":"http://127.0.0.1:18183"`) {
		t.Fatalf("intercept not rewritten: %s", body)
	}
	// Anything off the list never reaches the server, signed or not.
	count := len(server.seen)
	for _, path := range []string{"/api/org/users", "/api/config/../org", "/api/containers", "/api/sessions/s1/traces"} {
		if response, _ := do(t, "POST", front.URL+path, nil, "{}"); response.StatusCode != http.StatusNotFound {
			t.Fatalf("POST %s: %d", path, response.StatusCode)
		}
	}
	if response, _ := do(t, "DELETE", front.URL+"/api/sessions/s1", nil, ""); response.StatusCode != http.StatusNotFound {
		t.Fatalf("DELETE a session: %d", response.StatusCode)
	}
	if len(server.seen) != count {
		t.Fatal("a refused call reached the server")
	}
}

func TestOnlyProgramsOnThisComputerAreServed(t *testing.T) {
	server := newFakeServer(t, func(w http.ResponseWriter, r *http.Request) { _, _ = io.WriteString(w, "{}") })
	_, front := newProxy(t, server.URL)
	cases := []map[string]string{
		{"Host": "attacker.example:8184"},                        // DNS rebinding
		{"Origin": "http://attacker.example"},                    // another site's page
		{"Host": "192.168.1.5:8184"},                             // not loopback
		{"Origin": "http://127.0.0.1:9999", "Host": "127.0.0.1"}, // another local port's page
	}
	for _, headers := range cases {
		for _, path := range []string{"/api/auth/status", "/session/s1/v1/models", "/v1/models"} {
			if response, _ := do(t, "GET", front.URL+path, headers, ""); response.StatusCode != http.StatusForbidden {
				t.Fatalf("%v %s: %d", headers, path, response.StatusCode)
			}
		}
	}
	if len(server.seen) != 0 {
		t.Fatal("a refused request reached the server")
	}
}

func TestAgentTrafficStreamsWithoutTheMachinesToken(t *testing.T) {
	server := newFakeServer(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		flusher := w.(http.Flusher)
		_, _ = io.WriteString(w, "data: one\n\n")
		flusher.Flush()
		time.Sleep(200 * time.Millisecond)
		_, _ = io.WriteString(w, "data: two\n\n")
	})
	_, front := newProxy(t, server.URL)
	request, _ := http.NewRequest("POST", front.URL+"/session/s1/v1/chat/completions?x=1", strings.NewReader(`{"stream":true}`))
	request.Header.Set("Authorization", "Bearer agent-key")
	request.Header.Set("Cookie", "secret=1")
	response, err := settings.Client(5 * time.Second).Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	reader := bufio.NewReader(response.Body)
	began := time.Now()
	first, _ := reader.ReadString('\n')
	if first != "data: one\n" || time.Since(began) > 150*time.Millisecond {
		t.Fatalf("first event %q after %s: not streamed", first, time.Since(began))
	}
	rest, _ := io.ReadAll(reader)
	if !strings.Contains(string(rest), "data: two") {
		t.Fatalf("rest: %q", rest)
	}
	seen := server.last()
	if seen.URL.RequestURI() != "/session/s1/v1/chat/completions?x=1" {
		t.Fatalf("path %s", seen.URL.RequestURI())
	}
	// The agent's own Authorization goes through (hosted APIs need it); the machine's token and cookies don't.
	if seen.Header.Get("Authorization") != "Bearer agent-key" || seen.Header.Get(settings.TokenHeader) != "" || seen.Header.Get("Cookie") != "" {
		t.Fatalf("headers: %v", seen.Header)
	}
}

func TestTerminalRelayPassesTheServersRefusalOn(t *testing.T) {
	var gotToken string
	server := newFakeServer(t, func(w http.ResponseWriter, r *http.Request) {
		gotToken = r.Header.Get(settings.TokenHeader)
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		if strings.Contains(r.URL.Path, "/missing/") {
			_ = conn.WriteControl(websocket.CloseMessage, websocket.FormatCloseMessage(4404, "Unknown session"), time.Now().Add(time.Second))
			return
		}
		for {
			kind, data, err := conn.ReadMessage()
			if err != nil {
				return
			}
			_ = conn.WriteMessage(kind, append([]byte("echo:"), data...))
		}
	})
	_, front := newProxy(t, server.URL)
	base := "ws" + strings.TrimPrefix(front.URL, "http")
	conn, _, err := websocket.DefaultDialer.Dial(base+"/api/terminals/s1/host", nil)
	if err != nil {
		t.Fatal(err)
	}
	_ = conn.WriteMessage(websocket.BinaryMessage, []byte("hi"))
	_, data, err := conn.ReadMessage()
	if err != nil || string(data) != "echo:hi" || gotToken != "aip_machine" {
		t.Fatalf("relay: %q %v token %q", data, err, gotToken)
	}
	conn.Close()

	conn, _, err = websocket.DefaultDialer.Dial(base+"/api/terminals/missing/host", nil)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	_, _, err = conn.ReadMessage()
	closed, ok := err.(*websocket.CloseError)
	if !ok || closed.Code != 4404 || closed.Text != "Unknown session" {
		t.Fatalf("close: %v", err)
	}
}

func TestTunnelRefusedByTheServerAnswers407(t *testing.T) {
	server := newFakeServer(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusUnauthorized) })
	proxy, _ := newProxy(t, server.URL)
	listener, err := Listen(0)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	go func() { _ = proxy.ServeTunnels(listener) }()
	conn, err := net.Dial("tcp", listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	_, _ = io.WriteString(conn, "CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n")
	_ = conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	status, _ := bufio.NewReader(conn).ReadString('\n')
	if !strings.HasPrefix(status, "HTTP/1.1 407") {
		t.Fatalf("status %q", status)
	}
}

func TestHealthNamesTheServerAndUser(t *testing.T) {
	_, front := newProxy(t, "http://server.test:9999")
	_, body := do(t, "GET", front.URL+"/health", nil, "")
	var health map[string]any
	_ = json.Unmarshal([]byte(body), &health)
	if health["role"] != "agent-proxy" || health["server"] != "http://server.test:9999" || health["intercept_port"] != float64(18183) {
		t.Fatalf("health: %s", body)
	}
}
