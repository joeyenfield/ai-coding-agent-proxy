package presence

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"

	"ai-coding-agent-proxy/machine/internal/containers"
	"ai-coding-agent-proxy/machine/internal/providers"
	"ai-coding-agent-proxy/machine/internal/settings"
)

func TestTheMachineChecksEveryLaunchItself(t *testing.T) {
	root := t.TempDir()
	inside := filepath.Join(root, "api")
	if err := os.Mkdir(inside, 0o755); err != nil {
		t.Fatal(err)
	}
	machine := &settings.Machine{Launches: true, Roots: []string{root}}
	if project, err := Check(Launch{Project: inside}, machine); err != nil || !strings.HasSuffix(project, "api") {
		t.Fatalf("inside a root: %q %v", project, err)
	}
	outside := t.TempDir()
	for _, project := range []string{filepath.Join(root, "..", "elsewhere"), outside, filepath.Join(outside, "new"), filepath.Join(root, "new", "..", "..", "escape")} {
		if _, err := Check(Launch{Project: project}, machine); err == nil {
			t.Fatalf("%s accepted", project)
		}
	}
	// A new project folder inside a root is made for the launch.
	fresh := filepath.Join(root, "joey-testing", "sub")
	if project, err := Check(Launch{Project: fresh}, machine); err != nil || !strings.HasSuffix(project, filepath.Join("joey-testing", "sub")) {
		t.Fatalf("a new folder inside a root: %q %v", project, err)
	}
	if info, err := os.Stat(fresh); err != nil || !info.IsDir() {
		t.Fatalf("the new folder wasn't made: %v", err)
	}
	// A link inside a root that leads outside it doesn't let a new folder escape.
	if err := os.Symlink(outside, filepath.Join(root, "out")); err == nil {
		if _, err := Check(Launch{Project: filepath.Join(root, "out", "new")}, machine); err == nil {
			t.Fatal("a new folder through a link out of the root was accepted")
		}
		if _, err := os.Stat(filepath.Join(outside, "new")); err == nil {
			t.Fatal("a folder was made outside the root")
		}
	}
	machine.Launches = false
	if _, err := Check(Launch{Project: inside}, machine); err == nil || !strings.Contains(err.Error(), "launches on") {
		t.Fatalf("launches off: %v", err)
	}
}

func TestLaunchArgumentsRunTheAgentHeadless(t *testing.T) {
	args := Arguments(Launch{ID: "l1", Agent: "qwen", Model: "m", Visibility: "team", Team: "core", Access: "control", Trace: true}, "/work/api")
	want := []string{"qwen", "/work/api", "--headless", "--launch-id", "l1", "--share", "team", "--model", "m", "--trace", "--team", "core", "--let-others-type"}
	if !reflect.DeepEqual(args, want) {
		t.Fatalf("%v", args)
	}
	if args := Arguments(Launch{Agent: "qwen", Visibility: "everyone"}, "/w"); args[6] != "private" {
		t.Fatalf("unknown visibility: %v", args)
	}
}

func TestPresenceSaysHelloAndAnswersLaunches(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	messages := make(chan map[string]any, 10)
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/agents" {
			_, _ = w.Write([]byte(`[{"id":"go","executable":"go"},{"id":"nope","executable":"no-such-agent-here"}]`))
			return
		}
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		for {
			_, data, err := conn.ReadMessage()
			if err != nil {
				return
			}
			var message map[string]any
			_ = json.Unmarshal(data, &message)
			messages <- message
			if message["type"] == "hello" {
				_ = conn.WriteMessage(websocket.TextMessage, []byte(`{"type":"welcome"}`))
				_ = conn.WriteMessage(websocket.TextMessage, []byte(`{"type":"launch","launch":"l1","agent":"go","project":"/work"}`))
			}
		}
	}))
	defer server.Close()
	presence := &Presence{
		Upstream: settings.Upstream{Fixed: server.URL},
		Dial: func(base, path string) (*websocket.Conn, *http.Response, error) {
			return websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(base, "http")+path, nil)
		},
		Detect: func() []Provider { return []Provider{{Type: "ollama", Port: 11434}} },
		Launch: func(request Launch) (string, error) { return "session-for-" + request.ID, nil },
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go presence.Run(ctx)
	next := func() map[string]any {
		select {
		case message := <-messages:
			return message
		case <-time.After(10 * time.Second):
			t.Fatal("no message")
			return nil
		}
	}
	hello := next()
	if hello["type"] != "hello" || hello["id"] == "" || hello["launches"] != false {
		t.Fatalf("hello %v", hello)
	}
	// Only the agents whose executable is on PATH here.
	if agents, _ := hello["agents"].([]any); len(agents) != 1 || agents[0] != "go" {
		t.Fatalf("agents %v", hello["agents"])
	}
	launched := next()
	if launched["type"] != "launched" || launched["launch"] != "l1" || launched["session_id"] != "session-for-l1" {
		t.Fatalf("launched %v", launched)
	}
	if !strings.HasPrefix(presence.Status(), "connected to ") {
		t.Fatalf("status %q", presence.Status())
	}
}

func TestPresenceAnswersContainerRequests(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	messages := make(chan map[string]any, 10)
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/agents" {
			_, _ = w.Write([]byte(`[]`))
			return
		}
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		for {
			_, data, err := conn.ReadMessage()
			if err != nil {
				return
			}
			var message map[string]any
			_ = json.Unmarshal(data, &message)
			messages <- message
			if message["type"] == "hello" {
				_ = conn.WriteMessage(websocket.TextMessage, []byte(`{"type":"welcome"}`))
				_ = conn.WriteMessage(websocket.TextMessage, []byte(`{"type":"container","request":"r7","op":"start","name":"s1","spec":{"engine":"strata"}}`))
			}
		}
	}))
	defer server.Close()
	presence := &Presence{
		Upstream: settings.Upstream{Fixed: server.URL},
		Dial: func(base, path string) (*websocket.Conn, *http.Response, error) {
			return websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(base, "http")+path, nil)
		},
		Detect: func() []Provider { return nil },
		// Containers are off here: the request is answered, and refused.
		Containers: &containers.Manager{Machine: func() *settings.Machine { return &settings.Machine{} }},
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go presence.Run(ctx)
	next := func() map[string]any {
		select {
		case message := <-messages:
			return message
		case <-time.After(10 * time.Second):
			t.Fatal("no message")
			return nil
		}
	}
	hello := next()
	described, _ := hello["containers"].(map[string]any)
	// Whichever of docker and podman this computer has, found without running it.
	if described == nil || described["enabled"] != false || (described["cli"] != "docker" && described["cli"] != "podman") {
		t.Fatalf("hello containers %v", hello["containers"])
	}
	answer := next()
	if answer["type"] != "container_result" || answer["request"] != "r7" || answer["ok"] != false || !strings.Contains(answer["error"].(string), "agent-machine containers on") {
		t.Fatalf("answer %v", answer)
	}
}

func TestPresenceAnswersProviderRequestsAndSendsStrataKeys(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	profiles := t.TempDir()
	t.Setenv("STRATA_PROFILE_DIR", profiles)
	t.Setenv("STRATA_RUNTIME_DIR", t.TempDir())
	config := filepath.Join(profiles, "run.json")
	_ = os.WriteFile(config, []byte(`{"port": 18691, "api_key": "sk-strata"}`), 0o600)
	_ = os.WriteFile(filepath.Join(profiles, "coder.json"), []byte(`{"id": "coder", "source_config": `+strconv.Quote(config)+`}`), 0o600)
	ollama := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("{\"status\":\"pulling manifest\"}\n{\"status\":\"success\"}\n"))
	}))
	defer ollama.Close()
	port := ollama.Listener.Addr().(*net.TCPAddr).Port
	messages := make(chan map[string]any, 10)
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/agents" {
			_, _ = w.Write([]byte(`[]`))
			return
		}
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		for {
			_, data, err := conn.ReadMessage()
			if err != nil {
				return
			}
			var message map[string]any
			_ = json.Unmarshal(data, &message)
			messages <- message
			if message["type"] == "hello" {
				_ = conn.WriteMessage(websocket.TextMessage, []byte(`{"type":"welcome"}`))
				request, _ := json.Marshal(map[string]any{"type": "provider", "request": "p1", "op": "pull", "kind": "ollama", "port": port, "model": "qwen3"})
				_ = conn.WriteMessage(websocket.TextMessage, request)
			}
		}
	}))
	defer server.Close()
	presence := &Presence{
		Upstream: settings.Upstream{Fixed: server.URL},
		Dial: func(base, path string) (*websocket.Conn, *http.Response, error) {
			return websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(base, "http")+path, nil)
		},
		Detect:    func() []Provider { return []Provider{{Type: "strata", Port: 18691}, {Type: "ollama", Port: 11434}} },
		Providers: &providers.Manager{Machine: func() *settings.Machine { return &settings.Machine{Servers: true} }},
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go presence.Run(ctx)
	next := func() map[string]any {
		select {
		case message := <-messages:
			return message
		case <-time.After(10 * time.Second):
			t.Fatal("no message")
			return nil
		}
	}
	hello := next()
	found, _ := hello["providers"].([]any)
	if len(found) != 2 || found[0].(map[string]any)["api_key"] != "sk-strata" || found[1].(map[string]any)["api_key"] != nil {
		t.Fatalf("providers %v", hello["providers"])
	}
	var progress []any
	for {
		message := next()
		if message["type"] == "provider_progress" {
			if message["request"] != "p1" {
				t.Fatalf("%v", message)
			}
			progress = append(progress, message["progress"].(map[string]any)["status"])
			continue
		}
		if message["type"] != "provider_result" || message["request"] != "p1" || message["ok"] != true {
			t.Fatalf("answer %v", message)
		}
		break
	}
	if len(progress) != 2 || progress[1] != "success" {
		t.Fatalf("progress %v", progress)
	}
}
