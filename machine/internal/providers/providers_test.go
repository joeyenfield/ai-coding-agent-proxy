package providers

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"

	"ai-coding-agent-proxy/machine/internal/settings"
)

func on() *settings.Machine  { return &settings.Machine{Servers: true} }
func off() *settings.Machine { return &settings.Machine{} }

// upstream is a fake model server on loopback; it records the requests it got.
type upstream struct {
	*httptest.Server
	mu     sync.Mutex
	calls  []string
	bodies map[string]map[string]any
}

func serve(t *testing.T, routes map[string]http.HandlerFunc) *upstream {
	t.Helper()
	fake := &upstream{bodies: map[string]map[string]any{}}
	fake.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		key := r.Method + " " + r.URL.Path
		var body map[string]any
		data, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(data, &body)
		fake.mu.Lock()
		fake.calls = append(fake.calls, key)
		fake.bodies[key] = body
		fake.mu.Unlock()
		if handler, ok := routes[key]; ok {
			handler(w, r)
			return
		}
		http.NotFound(w, r)
	}))
	t.Cleanup(fake.Close)
	return fake
}

func (u *upstream) port() int {
	parsed, _ := url.Parse(u.URL)
	port, _ := strconv.Atoi(parsed.Port())
	return port
}

func reply(value any) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(value)
	}
}

func lines(values ...any) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		for _, value := range values {
			data, _ := json.Marshal(value)
			fmt.Fprintf(w, "%s\n", data)
		}
	}
}

func TestOllamaStatusMergesLoadedModels(t *testing.T) {
	fake := serve(t, map[string]http.HandlerFunc{
		"GET /api/tags": reply(map[string]any{"models": []any{
			map[string]any{"name": "qwen3:8b", "size": 5e9, "details": map[string]any{"family": "qwen3", "parameter_size": "8B", "quantization_level": "Q4_K_M"}},
			map[string]any{"name": "a:1b", "size": 1e9},
		}}),
		"GET /api/ps":      reply(map[string]any{"models": []any{map[string]any{"name": "qwen3:8b", "size": 6e9, "size_vram": 3e9, "context_length": 16384}}}),
		"GET /api/version": reply(map[string]any{"version": "0.12.3"}),
	})
	answer := (&Manager{Machine: off}).Handle(Request{ID: "r1", Op: "status", Kind: "ollama", Port: fake.port()}, nil)
	if answer["ok"] != true || answer["type"] != "provider_result" || answer["request"] != "r1" {
		t.Fatalf("%v", answer)
	}
	state := answer["state"].(map[string]any)
	models := state["models"].([]map[string]any)
	if state["online"] != true || state["version"] != "0.12.3" || len(models) != 2 || models[0]["name"] != "a:1b" {
		t.Fatalf("%v", state)
	}
	loaded := models[1]
	if loaded["loaded"] != true || loaded["gpu_percent"] != 50.0 || loaded["quantization"] != "Q4_K_M" || loaded["context_length"] != 16384.0 {
		t.Fatalf("%v", loaded)
	}
}

func TestOllamaOffline(t *testing.T) {
	fake := serve(t, nil)
	port := fake.port()
	fake.Close()
	state := (&Manager{}).Handle(Request{Op: "status", Kind: "ollama", Port: port}, nil)["state"].(map[string]any)
	if state["online"] != false || state["error"] == "" {
		t.Fatalf("%v", state)
	}
}

func TestChangesNeedServersOn(t *testing.T) {
	fake := serve(t, map[string]http.HandlerFunc{"DELETE /api/delete": reply(map[string]any{})})
	for _, op := range []string{"pull", "delete", "load", "unload"} {
		answer := (&Manager{Machine: off}).Handle(Request{ID: "x", Op: op, Kind: "ollama", Port: fake.port(), Model: "m"}, nil)
		if answer["ok"] != false || !strings.Contains(answer["error"].(string), "servers on") {
			t.Fatalf("%s: %v", op, answer)
		}
	}
	if len(fake.calls) != 0 {
		t.Fatalf("the model server was called: %v", fake.calls)
	}
	answer := (&Manager{Machine: on}).Handle(Request{Op: "delete", Kind: "ollama", Port: fake.port(), Model: "m"}, nil)
	if answer["ok"] != true || fake.bodies["DELETE /api/delete"]["model"] != "m" {
		t.Fatalf("%v %v", answer, fake.bodies)
	}
}

func TestRequestsAreChecked(t *testing.T) {
	manager := &Manager{Machine: on}
	for _, request := range []Request{
		{Op: "status", Kind: "vllm", Port: 1234},
		{Op: "status", Kind: "ollama", Port: 0},
		{Op: "status", Kind: "ollama", Port: 70000},
		{Op: "pull", Kind: "ollama", Port: 1234, Model: " "},
		{Op: "rm", Kind: "ollama", Port: 1234},
		{Op: "search", Catalog: "pypi"},
	} {
		if answer := manager.Handle(request, nil); answer["ok"] != false {
			t.Fatalf("%+v: %v", request, answer)
		}
	}
}

func TestOllamaPullRelaysProgress(t *testing.T) {
	fake := serve(t, map[string]http.HandlerFunc{
		"POST /api/pull": lines(map[string]any{"status": "pulling manifest"}, map[string]any{"status": "downloading", "digest": "sha", "total": 10, "completed": 5}, map[string]any{"status": "success"}),
	})
	var got []map[string]any
	answer := (&Manager{Machine: on}).Handle(Request{Op: "pull", Kind: "ollama", Port: fake.port(), Model: "qwen3"}, func(update map[string]any) { got = append(got, update) })
	if answer["ok"] != true || len(got) != 3 || got[1]["completed"] != 5.0 || fake.bodies["POST /api/pull"]["model"] != "qwen3" {
		t.Fatalf("%v %v", answer, got)
	}
	failing := serve(t, map[string]http.HandlerFunc{"POST /api/pull": lines(map[string]any{"error": "pull model manifest: file does not exist"})})
	got = nil
	answer = (&Manager{Machine: on}).Handle(Request{Op: "pull", Kind: "ollama", Port: failing.port(), Model: "nope"}, func(update map[string]any) { got = append(got, update) })
	if answer["ok"] != false || !strings.Contains(answer["error"].(string), "does not exist") || got[0]["status"] != "error" {
		t.Fatalf("%v %v", answer, got)
	}
}

func TestOllamaLoadAndUnload(t *testing.T) {
	fake := serve(t, map[string]http.HandlerFunc{"POST /api/generate": reply(map[string]any{"done": true})})
	manager := &Manager{Machine: on}
	if answer := manager.Handle(Request{Op: "unload", Kind: "ollama", Port: fake.port(), Model: "m"}, nil); answer["ok"] != true {
		t.Fatalf("%v", answer)
	}
	if body := fake.bodies["POST /api/generate"]; body["keep_alive"] != 0.0 || body["model"] != "m" {
		t.Fatalf("%v", body)
	}
	answer := manager.Handle(Request{Op: "load", Kind: "ollama", Port: fake.port(), Model: "m"}, nil)
	if answer["result"].(map[string]any)["loaded"] != "m" {
		t.Fatalf("%v", answer)
	}
}

func TestOllamaLogIsAgentMachines(t *testing.T) {
	home := t.TempDir()
	t.Setenv("AI_PROXY_MACHINE_HOME", home)
	manager := &Manager{}
	if answer := manager.Handle(Request{Op: "log", Kind: "ollama", Port: OllamaPort}, nil); answer["ok"] != true || !strings.Contains(answer["text"].(string), "No log yet") {
		t.Fatalf("%v", answer)
	}
	_ = os.WriteFile(filepath.Join(home, "ollama.log"), []byte("listening\n"), 0o600)
	if answer := manager.Handle(Request{Op: "log", Kind: "ollama", Port: OllamaPort}, nil); answer["text"] != "listening\n" {
		t.Fatalf("%v", answer)
	}
	if answer := manager.Handle(Request{Op: "log", Kind: "ollama", Port: 11500}, nil); answer["ok"] != false {
		t.Fatalf("%v", answer)
	}
}

func TestDockerStatusAndModels(t *testing.T) {
	fake := serve(t, map[string]http.HandlerFunc{
		"GET /engines/status": reply(map[string]any{"llama.cpp": "running"}),
		"GET /models": reply([]any{
			map[string]any{"id": "sha256:1", "tags": []any{"docker.io/ai/qwen3:latest", "ai/qwen3:8B"}, "created": 2, "config": map[string]any{"quantization": "Q4_K_M", "size": "5GB"}},
			map[string]any{"id": "sha256:2", "tags": []any{"ai/smollm2"}, "created": 1},
		}),
		"GET /engines/ps": reply([]any{map[string]any{"model_name": "ai/qwen3:8B", "backend_name": "llama.cpp", "in_use": true}}),
		"GET /engines/df": reply(map[string]any{"models_disk_usage": 5}),
	})
	state := (&Manager{}).Handle(Request{Op: "status", Kind: "docker", Port: fake.port()}, nil)["state"].(map[string]any)
	models := state["models"].([]map[string]any)
	if state["online"] != true || len(models) != 2 || models[0]["name"] != "ai/qwen3" || models[0]["loaded"] != true || models[0]["in_use"] != true || models[1]["loaded"] != false {
		t.Fatalf("%v", state)
	}
	if state["disk"].(map[string]any)["models_disk_usage"] != 5.0 {
		t.Fatalf("%v", state)
	}
	notDocker := serve(t, map[string]http.HandlerFunc{"GET /engines/status": reply(map[string]any{"x": 1})})
	if state := (&Manager{}).Handle(Request{Op: "status", Kind: "docker", Port: notDocker.port()}, nil)["state"].(map[string]any); state["online"] != false {
		t.Fatalf("%v", state)
	}
}

func TestDockerPullUnloadDelete(t *testing.T) {
	fake := serve(t, map[string]http.HandlerFunc{
		"POST /models/create": lines(
			map[string]any{"type": "progress", "total": 100, "layer": map[string]any{"id": "a", "current": 30}},
			map[string]any{"type": "progress", "total": 100, "layer": map[string]any{"id": "b", "current": 20}},
			map[string]any{"type": "success"},
		),
		"GET /models":                reply([]any{map[string]any{"id": "sha256:1", "tags": []any{"ai/qwen3:latest", "ai/qwen3:8B"}}}),
		"GET /engines/ps":            reply([]any{map[string]any{"model_name": "ai/qwen3:8B"}}),
		"POST /engines/unload":       reply(map[string]any{"unloaded_runners": 1}),
		"DELETE /models/ai/qwen3:8B": reply(map[string]any{}),
	})
	manager := &Manager{Machine: on}
	var got []map[string]any
	answer := manager.Handle(Request{Op: "pull", Kind: "docker", Port: fake.port(), Model: "ai/qwen3"}, func(update map[string]any) { got = append(got, update) })
	if answer["ok"] != true || len(got) != 3 || got[1]["completed"] != int64(50) || got[1]["total"] != int64(100) || got[2]["status"] != "success" {
		t.Fatalf("%v %v", answer, got)
	}
	answer = manager.Handle(Request{Op: "unload", Kind: "docker", Port: fake.port(), Model: "ai/qwen3"}, nil)
	if answer["ok"] != true || fmt.Sprint(fake.bodies["POST /engines/unload"]["models"]) != "[ai/qwen3:8B]" {
		t.Fatalf("%v %v", answer, fake.bodies)
	}
	if answer := manager.Handle(Request{Op: "delete", Kind: "docker", Port: fake.port(), Model: "ai/qwen3:8B"}, nil); answer["ok"] != true {
		t.Fatalf("%v", answer)
	}
}

func TestPullProgressMessages(t *testing.T) {
	layers := map[string]float64{}
	if update := pullProgress(map[string]any{"type": "error", "message": "denied"}, layers); update["error"] != "denied" {
		t.Fatalf("%v", update)
	}
	if update := pullProgress(map[string]any{"type": "other"}, layers); update != nil {
		t.Fatalf("%v", update)
	}
	if modelPath("hf.co/user/repo:Q4 K") != "hf.co/user/repo:Q4%20K" {
		t.Fatal(modelPath("hf.co/user/repo:Q4 K"))
	}
}

// strataFiles writes a profile, its config and (when alive) a ready file, like Strata does.
func strataFiles(t *testing.T, port int, key string, alive bool) (string, string) {
	t.Helper()
	profiles, runtimeDir := t.TempDir(), t.TempDir()
	t.Setenv("STRATA_PROFILE_DIR", profiles)
	t.Setenv("STRATA_RUNTIME_DIR", runtimeDir)
	t.Setenv("STRATA_API_KEY", "")
	engineLog := filepath.Join(runtimeDir, "engine.log")
	_ = os.WriteFile(engineLog, []byte("engine with "+key+" inside\n"), 0o600)
	config := filepath.Join(profiles, "run.json")
	write := func(path string, value any) {
		data, _ := json.Marshal(value)
		if err := os.WriteFile(path, data, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write(config, map[string]any{"host": "0.0.0.0", "port": port, "model_name": "qwen", "api_key": "none", "log": engineLog})
	write(filepath.Join(profiles, "coder.json"), map[string]any{"id": "coder", "source_config": config, "overrides": map[string]any{"api_key": key + ",second"}})
	if alive {
		write(filepath.Join(runtimeDir, "coder-ab12.ready.json"), map[string]any{"api_url": fmt.Sprintf("http://127.0.0.1:%d", port), "model_id": "qwen-live", "model_state": "loaded", "pid": os.Getpid(), "created_at": 1})
		write(filepath.Join(runtimeDir, "coder-dead.ready.json"), map[string]any{"api_url": "http://127.0.0.1:1", "pid": 999999999, "created_at": 2})
		_ = os.WriteFile(filepath.Join(runtimeDir, "coder.server.log"), []byte("POST /v1/chat key="+key+"\n"), 0o600)
	}
	return profiles, runtimeDir
}

func TestStrataDiscovery(t *testing.T) {
	strataFiles(t, 18601, "sk-one", true)
	found := Discover()
	if len(found) != 1 {
		t.Fatalf("%v", found)
	}
	instance := found[0]
	if instance.Profile != "coder" || !instance.Running || instance.Model != "qwen-live" || instance.APIKey != "sk-one" || instance.Port() != 18601 || instance.ServerLog == "" || instance.EngineLog == "" {
		t.Fatalf("%+v", instance)
	}
	public := instance.Public()
	if _, leaked := public["api_key"]; leaked || public["has_key"] != true {
		t.Fatalf("%v", public)
	}
	strataFiles(t, 18602, "sk-two", false)
	stopped := InstanceOn(18602)
	if stopped == nil || stopped.Running || stopped.URL != "http://127.0.0.1:18602" || stopped.Model != "qwen" {
		t.Fatalf("%+v", stopped)
	}
	if FirstKey([]any{"", " k "}) != "k" || FirstKey("none") != "" || FirstKey(nil) != "" {
		t.Fatal("first key")
	}
}

func TestStrataStatusKeyAndLog(t *testing.T) {
	var sawKey string
	fake := serve(t, map[string]http.HandlerFunc{
		"GET /health": reply(map[string]any{"service": "strata", "api_key": true, "model": "qwen", "loaded": true}),
		"GET /v1/status": func(w http.ResponseWriter, r *http.Request) {
			sawKey = r.Header.Get("Authorization")
			reply(map[string]any{"engine": "strata 1.2"})(w, r)
		},
		"GET /v1/models":  reply(map[string]any{"data": []any{map[string]any{"id": "qwen", "status": map[string]any{"value": "loaded"}}, map[string]any{"x": 1}}}),
		"POST /v1/unload": reply(map[string]any{"status": "unloading"}),
	})
	strataFiles(t, fake.port(), "sk-one", true)
	manager := &Manager{Machine: on}
	state := manager.Handle(Request{Op: "status", Kind: "strata", Port: fake.port()}, nil)["state"].(map[string]any)
	key := state["key"].(map[string]any)
	if state["online"] != true || key["state"] != "ok" || key["source"] != "profile coder" || sawKey != "Bearer sk-one" || len(state["models"].([]any)) != 1 || state["instance"].(map[string]any)["profile"] != "coder" {
		t.Fatalf("%v %s", state, sawKey)
	}
	// The server's key for its containers comes first.
	state = manager.Handle(Request{Op: "status", Kind: "strata", Port: fake.port(), Key: "sk-container"}, nil)["state"].(map[string]any)
	if state["key"].(map[string]any)["source"] != "container" || sawKey != "Bearer sk-container" {
		t.Fatalf("%v", state)
	}
	answer := manager.Handle(Request{Op: "unload", Kind: "strata", Port: fake.port()}, nil)
	if answer["ok"] != true || answer["result"].(map[string]any)["status"] != "unloading" {
		t.Fatalf("%v", answer)
	}
	log := manager.Handle(Request{Op: "log", Kind: "strata", Port: fake.port()}, nil)
	if text := log["text"].(string); strings.Contains(text, "sk-one") || !strings.Contains(text, "<api key>") {
		t.Fatalf("%v", log)
	}
	engine := manager.Handle(Request{Op: "log", Kind: "strata", Port: fake.port(), Source: "engine"}, nil)
	if !strings.Contains(engine["text"].(string), "engine with <api key>") {
		t.Fatalf("%v", engine)
	}
	if answer := manager.Handle(Request{Op: "log", Kind: "strata", Port: 1}, nil); answer["ok"] != false {
		t.Fatalf("%v", answer)
	}
}

func TestStrataMissingKey(t *testing.T) {
	fake := serve(t, map[string]http.HandlerFunc{
		"GET /health":    reply(map[string]any{"service": "strata", "api_key": true, "model": "qwen", "max_context": 8192}),
		"GET /v1/status": func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusUnauthorized) },
		"GET /v1/models": func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusUnauthorized) },
	})
	t.Setenv("STRATA_PROFILE_DIR", t.TempDir())
	t.Setenv("STRATA_API_KEY", "")
	state := (&Manager{}).Handle(Request{Op: "status", Kind: "strata", Port: fake.port()}, nil)["state"].(map[string]any)
	models := state["models"].([]any)
	if state["key"].(map[string]any)["state"] != "missing" || len(models) != 1 || models[0].(map[string]any)["id"] != "qwen" {
		t.Fatalf("%v", state)
	}
}

const libraryPage = `<ul><li><a href="/library/qwen3" class="group w-full">
<p class="max-w-lg" title="Qwen3 &amp; friends">x</p>
<span class="text-indigo-600 x">tools</span><span class="text-indigo-600">thinking</span>
<span class="text-blue-600">8B</span><span class="text-blue-600">8b</span><span class="text-blue-600">32B</span>
<span title="1,234 downloads">1.2K</span></a></li>
<li><a href="/library/user/x" class="group">skip</a></li></ul>`

func TestCatalogParsers(t *testing.T) {
	models := ParseLibrary(libraryPage)
	if len(models) != 1 || models[0]["description"] != "Qwen3 & friends" || fmt.Sprint(models[0]["sizes"]) != "[8b 32b]" || models[0]["pulls"] != 1234 || fmt.Sprint(models[0]["capabilities"]) != "[tools thinking]" {
		t.Fatalf("%v", models)
	}
	for stem, want := range map[string]string{"Qwen3-8B-UD-Q4_K_XL": "UD-Q4_K_XL", "model-q8_0": "Q8_0", "big-BF16-00001-of-00002": "BF16", "readme": "", "Qwen3-8B": ""} {
		if got := QuantFromFilename(stem); got != want {
			t.Fatalf("%s: %q, want %q", stem, got, want)
		}
	}
	quants := ParseQuants([]any{
		map[string]any{"type": "file", "path": "Q8/m-Q8_0-00001-of-00002.gguf", "lfs": map[string]any{"size": 50}},
		map[string]any{"type": "file", "path": "Q8/m-Q8_0-00002-of-00002.gguf", "lfs": map[string]any{"size": 50}},
		map[string]any{"type": "file", "path": "m-Q4_K_M.gguf", "size": 40},
		map[string]any{"type": "file", "path": "mmproj-F16.gguf", "size": 1},
		map[string]any{"type": "directory", "path": "Q8"},
	})
	if len(quants) != 2 || quants[0]["quant"] != "Q4_K_M" || quants[1]["size"] != int64(100) || quants[1]["split"] != true {
		t.Fatalf("%v", quants)
	}
	tags := ParseHubTags(map[string]any{"results": []any{
		map[string]any{"name": "8B-Q8_0", "full_size": 9},
		map[string]any{"name": "8B-Q4_K_M", "full_size": 5},
		map[string]any{"name": "latest", "full_size": 5},
		map[string]any{"name": "8B-safetensors", "full_size": 16},
	}})
	if tags[0]["tag"] != "latest" || tags[1]["tag"] != "8B-Q4_K_M" || tags[3]["engine"] != "vllm" || tags[1]["engine"] != nil || TagEngine("4B-mlx") != "mlx" {
		t.Fatalf("%v", tags)
	}
}

func TestSearchCatalogs(t *testing.T) {
	var hubCalls int
	fake := serve(t, map[string]http.HandlerFunc{
		"GET /search": func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Query().Get("q") != "qwen" || !strings.Contains(r.UserAgent(), "agent-machine") {
				w.WriteHeader(400)
				return
			}
			_, _ = io.WriteString(w, libraryPage)
		},
		"GET /api/models":                      reply([]any{map[string]any{"id": "unsloth/Qwen3-GGUF", "downloads": 10}}),
		"GET /api/models/gated/repo/tree/main": func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(403) },
		"GET /v2/repositories/ai/": func(w http.ResponseWriter, r *http.Request) {
			hubCalls++
			reply(map[string]any{"results": []any{map[string]any{"name": "qwen3", "namespace": "ai", "pull_count": 7}}})(w, r)
		},
		"GET /v2/repositories/ai/qwen3/tags": reply(map[string]any{"results": []any{map[string]any{"name": "latest"}}}),
	})
	manager := &Manager{LibraryURL: fake.URL + "/search", HuggingFaceURL: fake.URL + "/api/models", DockerHubURL: fake.URL + "/v2"}
	search := func(catalog, query, repo string) map[string]any {
		answer := manager.Handle(Request{Op: "search", Catalog: catalog, Query: query, Repo: repo}, nil)
		if answer["ok"] != true {
			t.Fatalf("%s: %v", catalog, answer)
		}
		return answer["result"].(map[string]any)
	}
	if result := search("ollama", "qwen", ""); len(result["models"].([]map[string]any)) != 1 || result["note"] != nil {
		t.Fatalf("%v", result)
	}
	if result := search("huggingface", "qwen", ""); result["repos"].([]map[string]any)[0]["repo"] != "unsloth/Qwen3-GGUF" {
		t.Fatalf("%v", result)
	}
	if result := search("huggingface", "", ""); result["note"] == nil {
		t.Fatalf("%v", result)
	}
	gated := manager.Handle(Request{Op: "search", Catalog: "huggingface-quants", Repo: "gated/repo"}, nil)
	if gated["ok"] != false || !strings.Contains(gated["error"].(string), "gated") {
		t.Fatalf("%v", gated)
	}
	search("docker-hub", "Qwen", "")
	if result := search("docker-hub", "qwen", ""); result["models"].([]map[string]any)[0]["name"] != "ai/qwen3" || hubCalls != 1 {
		t.Fatalf("%v (calls %d)", result, hubCalls)
	}
	if result := search("docker-hub-tags", "", "ai/qwen3"); len(result["tags"].([]map[string]any)) != 1 {
		t.Fatalf("%v", result)
	}
	if answer := manager.Handle(Request{Op: "search", Catalog: "docker-hub-tags", Repo: "../etc"}, nil); answer["ok"] != false {
		t.Fatalf("%v", answer)
	}
}

func TestMetrics(t *testing.T) {
	answer := (&Manager{}).Handle(Request{Op: "metrics"}, nil)
	metrics := answer["metrics"].(map[string]any)
	cpu := metrics["cpu"].(map[string]any)
	if answer["ok"] != true || cpu["count"].(int) < 1 || metrics["hostname"] == "" || metrics["gpus"] == nil {
		t.Fatalf("%v", answer)
	}
	if encoded, err := json.Marshal(answer); err != nil {
		t.Fatal(err)
	} else {
		t.Logf("%s", encoded)
	}
	gpus := ParseNvidia([]byte("0, NVIDIA RTX 4090, 37, 1024, 24564, 51, [N/A]\nbad line\n"))
	if len(gpus) != 1 || gpus[0]["memory_used"] != int64(1024*1024*1024) || gpus[0]["power_watts"] != nil || gpus[0]["utilization"] != 37.0 {
		t.Fatalf("%v", gpus)
	}
}
