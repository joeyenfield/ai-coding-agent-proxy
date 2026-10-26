package presence

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"

	"ai-coding-agent-proxy/machine/internal/settings"
)

func TestTheMachineSaysItsVersionAndHearsTheServers(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	settings.Version = "0.4.0"
	t.Cleanup(func() { settings.Version = "dev" })
	headers := make(chan http.Header, 10)
	hellos := make(chan map[string]any, 1)
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		headers <- r.Header.Clone()
		switch r.URL.Path {
		case "/health":
			_, _ = w.Write([]byte(`{"status":"ok","version":"0.5.0","protocol":1,"min_protocol":1}`))
			return
		case "/api/agents":
			_, _ = w.Write([]byte(`[]`))
			return
		}
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		_, data, _ := conn.ReadMessage()
		var hello map[string]any
		_ = json.Unmarshal(data, &hello)
		hellos <- hello
		_ = conn.WriteMessage(websocket.TextMessage, []byte(`{"type":"welcome"}`))
		_, _, _ = conn.ReadMessage()
	}))
	defer server.Close()
	heard := make(chan string, 1)
	presence := &Presence{
		Upstream:        settings.Upstream{Fixed: server.URL},
		Detect:          func() []Provider { return nil },
		OnServerVersion: func(version string) { heard <- version },
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go presence.Run(ctx)
	select {
	case hello := <-hellos:
		if hello["protocol"] != float64(settings.Protocol) || hello["version"] != "0.4.0" {
			t.Fatalf("hello %v", hello)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("no hello")
	}
	select {
	case version := <-heard:
		if version != "0.5.0" {
			t.Fatalf("heard %q", version)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("didn't ask the server's version")
	}
	cancel()
	// Every request to the server, the fleet socket's included, says which agent-machine it is.
	for len(headers) > 0 {
		header := <-headers
		if header.Get(settings.VersionHeader) != "0.4.0" || header.Get(settings.ProtocolHeader) != strconv.Itoa(settings.Protocol) || header.Get("User-Agent") != "agent-machine/0.4.0" {
			t.Errorf("unidentified request: %v", header)
		}
	}
	if notice := VersionNotice("0.4.0", "0.5.0"); !strings.Contains(notice, "agent-machine update") {
		t.Errorf("notice %q", notice)
	}
	if VersionNotice("dev", "0.5.0") != "" || VersionNotice("0.4.0", "0.4.0") != "" || VersionNotice("0.4.0", "") != "" {
		t.Error("a notice with nothing to update")
	}
}

func TestAServerThatCantTakeThisMachineSaysWhy(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/machines/connect" {
			_, _ = w.Write([]byte(`[]`))
			return
		}
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		_, _, _ = conn.ReadMessage()
		_ = conn.WriteMessage(websocket.CloseMessage, websocket.FormatCloseMessage(CloseUpgradeRequired, "agent-machine dev speaks protocol 1; this agent-server needs 2..2"))
		time.Sleep(100 * time.Millisecond)
	}))
	defer server.Close()
	presence := &Presence{Upstream: settings.Upstream{Fixed: server.URL}, Detect: func() []Provider { return nil }}
	presence.Run(contextFor(t, presence))
	if status := presence.Status(); !strings.Contains(status, "can't take this agent-machine") || !strings.Contains(status, "needs 2..2") {
		t.Fatalf("status %q", status)
	}
}

// contextFor ends once the presence has failed to connect once.
func contextFor(t *testing.T, presence *Presence) context.Context {
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	presence.OnStatus = func(status string) {
		if strings.HasPrefix(status, "disconnected") {
			cancel()
		}
	}
	return ctx
}
