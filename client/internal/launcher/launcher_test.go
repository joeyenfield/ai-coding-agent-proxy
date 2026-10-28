package launcher

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestArgumentsGoToTheLauncherOrTheAgent(t *testing.T) {
	options, err := ParseArgs([]string{"qwen", "proj", "--model", "qwen3.6:35b", "--yolo", "--trace", "--share=team", "--team", "core", "--", "--model", "x"})
	if err != nil {
		t.Fatal(err)
	}
	if options.Agent != "qwen" || options.Project != "proj" || options.Model != "qwen3.6:35b" || !options.Trace {
		t.Fatalf("%+v", options)
	}
	if !reflect.DeepEqual(options.Extra, []string{"--yolo", "--model", "x"}) {
		t.Fatalf("extra %v", options.Extra)
	}
	if sharing := options.Sharing(); sharing["visibility"] != "team" || sharing["team"] != "core" || sharing["access"] != "view" {
		t.Fatalf("sharing %v", sharing)
	}
	options, _ = ParseArgs([]string{"claude", "-m=sonnet", "--control", "--let-others-type", "--no-remote"})
	if options.Project != "." || options.Model != "sonnet" || !options.Control || options.Sharing()["access"] != "control" || len(options.Extra) != 0 {
		t.Fatalf("%+v", options)
	}
	options, _ = ParseArgs([]string{"qwen", "--mcp", "desk/github, desk/docs", "--mcp=laptop"})
	if !reflect.DeepEqual(options.MCP, []string{"desk/github", "desk/docs", "laptop"}) || len(options.Extra) != 0 {
		t.Fatalf("mcp %v, extra %v", options.MCP, options.Extra)
	}
	if _, err := ParseArgs([]string{"qwen", "--share", "everyone"}); err == nil {
		t.Fatal("--share everyone accepted")
	}
	if _, err := ParseArgs([]string{"qwen", "--model"}); err == nil {
		t.Fatal("--model without a value accepted")
	}
}

func TestAShellsOneArgumentIsItsFolder(t *testing.T) {
	// As agent-machine runs it for a terminal from the console: the flag before or after the folder.
	for _, args := range [][]string{{"--shell", "/work/api", "--headless", "--launch-id", "l9"}, {"/work/api", "--shell", "--headless"}} {
		options, err := ParseArgs(args)
		if err != nil || !options.Shell || options.Agent != "" || options.Project != "/work/api" || !options.Headless {
			t.Fatalf("%v: %+v %v", args, options, err)
		}
	}
	if options, _ := ParseArgs([]string{"--shell"}); options.Project != "." || options.Agent != "" {
		t.Fatalf("no folder: %+v", options)
	}
	if _, err := ParseArgs([]string{"--shell", "/a", "/b"}); err == nil {
		t.Fatal("two folders accepted")
	}
	command, err := ShellCommand()
	if err != nil || len(command) == 0 {
		t.Fatalf("no shell here: %v %v", command, err)
	}
}

func TestEnvironmentOverridesReplaceInheritedValues(t *testing.T) {
	merged := mergeEnv([]string{"PATH=/bin", "Https_Proxy=old", "KEEP=1"}, map[string]string{"HTTPS_PROXY": "new", "AI_PROXY_SESSION": "s1"})
	joined := strings.Join(merged, "\n")
	if !strings.Contains(joined, "HTTPS_PROXY=new") || !strings.Contains(joined, "KEEP=1") || !strings.Contains(joined, "AI_PROXY_SESSION=s1") {
		t.Fatal(joined)
	}
	// Windows names are case-insensitive, so the inherited spelling goes.
	if strings.Contains(joined, "Https_Proxy=old") != (runtime.GOOS != "windows") {
		t.Fatal(joined)
	}
}

func TestEnsureContainerStartsTheBackendAndWaitsUntilItServes(t *testing.T) {
	containerPoll = 10 * time.Millisecond
	var mu sync.Mutex
	polls, started := 0, false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		switch {
		case r.Method == "POST" && r.URL.Path == "/api/containers/box/start":
			started = true
			_, _ = w.Write([]byte(`{}`))
		case r.Method == "POST" && r.URL.Path == "/api/containers/other/start":
			w.WriteHeader(http.StatusConflict)
			_, _ = w.Write([]byte(`{"detail":"strata-iq3 uses that GPU."}`))
		case r.URL.Path == "/api/containers/box":
			polls++
			phase := map[bool]string{false: "stopped", true: "loading"}[started]
			_ = json.NewEncoder(w).Encode(map[string]any{"ready": started && polls > 3, "phase": phase, "engine_label": "Ollama"})
		case r.URL.Path == "/api/containers/other":
			_ = json.NewEncoder(w).Encode(map[string]any{"ready": false, "phase": "stopped", "engine_label": "Strata"})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	if err := EnsureContainer(server.URL, "box", false, time.Minute); err != nil {
		t.Fatal(err)
	}
	if !started {
		t.Fatal("not started")
	}
	err := EnsureContainer(server.URL, "other", false, time.Minute)
	if err == nil || !strings.Contains(err.Error(), "--switch") {
		t.Fatalf("err %v", err)
	}
}

func TestVersionNoticesSayWhatToUpdate(t *testing.T) {
	same := map[string]any{"version": "0.4.0"}
	if lines := VersionNotices("0.4.0", same, same); len(lines) != 0 {
		t.Fatalf("same versions: %v", lines)
	}
	lines := VersionNotices("0.4.0", map[string]any{}, map[string]any{"version": "0.5.0"})
	if len(lines) != 2 || !strings.Contains(lines[0], "agent proxy stop") || !strings.Contains(lines[1], "agent update") {
		t.Fatalf("got %v", lines)
	}
	// A build without a version, or a server that doesn't say, gives no notice about the server.
	if lines := VersionNotices("dev", map[string]any{}, map[string]any{"version": "0.5.0"}); len(lines) != 0 {
		t.Fatalf("dev build: %v", lines)
	}
	if lines := VersionNotices("0.4.0", same, nil); len(lines) != 0 {
		t.Fatalf("silent server: %v", lines)
	}
}
