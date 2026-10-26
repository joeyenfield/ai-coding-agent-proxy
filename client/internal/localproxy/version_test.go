package localproxy

import (
	"encoding/json"
	"net/http"
	"strconv"
	"testing"
	"time"

	"ai-coding-agent-proxy/client/internal/settings"
)

func TestEverythingForwardedSaysWhichClientItIs(t *testing.T) {
	settings.Version = "0.4.0+abc1234"
	t.Cleanup(func() { settings.Version = "dev" })
	server := newFakeServer(t, func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true})
	})
	_, front := newProxy(t, server.URL)
	for _, path := range []string{"/api/agents", "/session/s1/v1/chat/completions"} {
		method := http.MethodGet
		if path != "/api/agents" {
			method = http.MethodPost
		}
		response, _ := do(t, method, front.URL+path, map[string]string{"User-Agent": "qwen-code/1.0"}, "{}")
		if response.StatusCode != http.StatusOK {
			t.Fatalf("%s: %d", path, response.StatusCode)
		}
		seen := server.last()
		if seen.Header.Get(settings.VersionHeader) != "0.4.0+abc1234" || seen.Header.Get(settings.ProtocolHeader) != strconv.Itoa(settings.Protocol) {
			t.Errorf("%s reached the server without the client's version: %v", path, seen.Header)
		}
		// The agent's own User-Agent goes on: the server records which agent sent it.
		if got := seen.Header.Get("User-Agent"); got != "qwen-code/1.0" {
			t.Errorf("%s: User-Agent %q", path, got)
		}
	}
}

func TestTheServersRefusalOfAnOldClientReachesTheLauncher(t *testing.T) {
	server := newFakeServer(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUpgradeRequired)
		_ = json.NewEncoder(w).Encode(map[string]any{"detail": "agent 0.1.0 speaks protocol 1; this agent-server (0.9.0) needs 2..2"})
	})
	_, front := newProxy(t, server.URL)
	response, body := do(t, http.MethodGet, front.URL+"/api/agents", nil, "")
	if response.StatusCode != http.StatusUpgradeRequired || settings.Detail([]byte(body)) != "agent 0.1.0 speaks protocol 1; this agent-server (0.9.0) needs 2..2" {
		t.Fatalf("got %d %s", response.StatusCode, body)
	}
}

func TestHealthSaysTheProxysVersion(t *testing.T) {
	settings.Version = "0.4.0"
	t.Cleanup(func() { settings.Version = "dev" })
	_, front := newProxy(t, "http://127.0.0.1:1")
	_, body := do(t, http.MethodGet, front.URL+"/health", nil, "")
	var health map[string]any
	_ = json.Unmarshal([]byte(body), &health)
	if health["version"] != "0.4.0" || health["protocol"] != float64(settings.Protocol) {
		t.Fatalf("health: %v", health)
	}
}

func TestOnlyProgramsOnThisComputerCanStopTheProxy(t *testing.T) {
	proxy, front := newProxy(t, "http://127.0.0.1:1")
	stopped := make(chan struct{}, 1)
	proxy.Shutdown = func() { stopped <- struct{}{} }
	for _, refused := range []struct {
		name    string
		method  string
		headers map[string]string
		status  int
	}{
		{"another site's page", http.MethodPost, map[string]string{"Origin": "http://evil.example"}, http.StatusForbidden},
		{"DNS rebinding", http.MethodPost, map[string]string{"Host": "evil.example:8184"}, http.StatusForbidden},
		{"a GET", http.MethodGet, nil, http.StatusMethodNotAllowed},
	} {
		if response, _ := do(t, refused.method, front.URL+"/shutdown", refused.headers, ""); response.StatusCode != refused.status {
			t.Errorf("%s: got %d, want %d", refused.name, response.StatusCode, refused.status)
		}
	}
	select {
	case <-stopped:
		t.Fatal("stopped for a refused request")
	default:
	}
	if response, _ := do(t, http.MethodPost, front.URL+"/shutdown", nil, ""); response.StatusCode != http.StatusOK {
		t.Fatalf("got %d", response.StatusCode)
	}
	select {
	case <-stopped:
	case <-time.After(2 * time.Second):
		t.Fatal("didn't stop")
	}
}
