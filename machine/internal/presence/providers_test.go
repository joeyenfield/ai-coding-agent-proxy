package presence

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"strconv"
	"testing"
)

func fakeServer(t *testing.T, path string, status int, body any) int {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != path {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(body)
	}))
	t.Cleanup(server.Close)
	parsed, _ := url.Parse(server.URL)
	port, _ := strconv.Atoi(parsed.Port())
	return port
}

func TestDetectionFindsOpenAICompatibleServersByTheirModelList(t *testing.T) {
	models := map[string]any{"object": "list", "data": []any{map[string]any{"id": "qwen3-coder"}, map[string]any{"id": "gemma"}}}
	ollama := fakeServer(t, "/api/version", http.StatusOK, map[string]any{"version": "0.12.3"})
	lmstudio := fakeServer(t, "/v1/models", http.StatusOK, models)
	// Something else on 8000: it answers, but not with a model list.
	other := fakeServer(t, "/v1/models", http.StatusOK, map[string]any{"status": "ok"})
	notIDs := fakeServer(t, "/v1/models", http.StatusOK, map[string]any{"data": []any{"a", "b"}})
	refused := fakeServer(t, "/v1/models", http.StatusUnauthorized, models)
	sglang := fakeServer(t, "/v1/models", http.StatusOK, map[string]any{"data": []any{}})
	named := fakeServer(t, "/v1/models", http.StatusOK, models)

	savedProbes, savedAPI := probes, apiProbes
	t.Cleanup(func() { probes, apiProbes = savedProbes, savedAPI })
	probes = append(probes[:0:0], probes[0])
	probes[0].port = ollama
	apiProbes = []struct {
		flavor string
		port   int
	}{{"lmstudio", lmstudio}, {"vllm", other}, {"vllm", notIDs}, {"vllm", refused}, {"sglang", sglang}}
	// The environment's ports: bad entries are ignored, and a port already found isn't reported twice.
	t.Setenv(APIPortsEnvironment, "nope, "+strconv.Itoa(named)+",70000,"+strconv.Itoa(lmstudio)+","+strconv.Itoa(ollama))

	two, none := 2, 0
	want := []Provider{
		{Type: "ollama", Port: ollama, Version: "0.12.3"},
		{Type: "api", Port: lmstudio, Flavor: "lmstudio", Models: &two},
		{Type: "api", Port: sglang, Flavor: "sglang", Models: &none},
		{Type: "api", Port: named, Flavor: "openai", Models: &two},
	}
	found := DetectProviders()
	// Ollama's model count comes from /api/tags, which this fake doesn't serve.
	if !reflect.DeepEqual(found, want) {
		data, _ := json.Marshal(found)
		t.Fatalf("%s", data)
	}
	data, _ := json.Marshal(found[1])
	if string(data) != `{"type":"api","port":`+strconv.Itoa(lmstudio)+`,"models":2,"flavor":"lmstudio"}` {
		t.Fatalf("%s", data)
	}
}
