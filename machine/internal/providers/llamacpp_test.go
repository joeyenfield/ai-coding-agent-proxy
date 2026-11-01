package providers

import (
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// router plays llama-server's router: two models, one loaded, and a download that reports
// progress on /models/sse once it's asked for.
func router(t *testing.T, key string, outcome string) *upstream {
	t.Helper()
	dir := t.TempDir()
	model := filepath.Join(dir, "qwen.gguf")
	_ = os.WriteFile(model, make([]byte, 1234), 0o600)
	started := make(chan struct{}, 1)
	guard := func(handler http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			if key != "" && r.Header.Get("Authorization") != "Bearer "+key {
				w.WriteHeader(http.StatusUnauthorized)
				fmt.Fprint(w, `{"error":{"code":401,"message":"Invalid API Key"}}`)
				return
			}
			handler(w, r)
		}
	}
	return serve(t, map[string]http.HandlerFunc{
		"GET /models": guard(reply(map[string]any{"data": []any{
			map[string]any{"id": "unsloth/zeta-GGUF:Q4_K_M", "path": "", "status": map[string]any{"value": "unloaded", "failed": true}},
			map[string]any{"id": "ggml-org/qwen-GGUF:Q8_0", "path": model, "status": map[string]any{"value": "loaded"}, "meta": map[string]any{"n_ctx_train": 32768}},
			map[string]any{"id": "big/one-GGUF:Q4_K_M", "status": map[string]any{"value": "downloading", "progress": map[string]any{"a": map[string]any{"done": 5, "total": 10}, "b": map[string]any{"done": 5, "total": 10}}}},
		}})),
		"POST /models": guard(func(w http.ResponseWriter, r *http.Request) {
			started <- struct{}{}
			reply(map[string]any{"success": true})(w, r)
		}),
		"GET /models/sse": guard(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/event-stream")
			w.(http.Flusher).Flush()
			select {
			case <-started:
			case <-time.After(5 * time.Second):
				return
			}
			for _, line := range []string{
				`{"model":"someone/else-GGUF:Q4_K_M","event":"download_finished","data":{}}`,
				`{"model":"ggml-org/new-GGUF:Q4_K_M","event":"download_progress","data":{"u1":{"done":40,"total":100},"u2":{"done":10,"total":100}}}`,
				outcome,
			} {
				fmt.Fprintf(w, "data: %s\n\n", line)
				w.(http.Flusher).Flush()
			}
		}),
		"DELETE /models":      guard(reply(map[string]any{"success": true})),
		"POST /models/load":   guard(reply(map[string]any{"success": true})),
		"POST /models/unload": guard(reply(map[string]any{"success": false, "error": map[string]any{"message": "model is not loaded"}})),
	})
}

func TestLlamaStatusListsTheRoutersModels(t *testing.T) {
	fake := router(t, "", "")
	manager := &Manager{Machine: off}
	answer := manager.Handle(Request{Op: "status", Kind: "llamacpp", Port: fake.port()}, nil)
	state := answer["state"].(map[string]any)
	models := state["models"].([]map[string]any)
	if state["online"] != true || state["router"] != true || len(models) != 3 {
		t.Fatalf("%v", state)
	}
	if models[0]["name"] != "ggml-org/qwen-GGUF:Q8_0" || models[0]["loaded"] != true || models[0]["size"] != int64(1234) || models[0]["context_train"] != float64(32768) {
		t.Fatalf("loaded first, with its file's size: %v", models[0])
	}
	if downloading := models[1]; downloading["state"] != "downloading" || downloading["progress"].(map[string]any)["completed"] != int64(10) {
		t.Fatalf("%v", downloading)
	}
	if models[2]["failed"] != true {
		t.Fatalf("%v", models[2])
	}
	if offline := (&Manager{Machine: off}).Handle(Request{Op: "status", Kind: "llamacpp", Port: 1}, nil)["state"].(map[string]any); offline["online"] != false {
		t.Fatalf("%v", offline)
	}
}

func TestLlamaPullRelaysTheDownloadsProgress(t *testing.T) {
	fake := router(t, "k3y", `{"model":"ggml-org/new-GGUF:Q4_K_M","event":"download_finished","data":{}}`)
	manager := &Manager{Machine: on}
	var updates []map[string]any
	answer := manager.Handle(Request{Op: "pull", Kind: "llamacpp", Port: fake.port(), Model: "ggml-org/new-GGUF:Q4_K_M", Key: "k3y"}, func(update map[string]any) { updates = append(updates, update) })
	if answer["ok"] != true {
		t.Fatalf("%v", answer)
	}
	if fake.bodies["POST /models"]["model"] != "ggml-org/new-GGUF:Q4_K_M" {
		t.Fatalf("asked %v", fake.bodies["POST /models"])
	}
	// Another model's events aren't this pull's.
	if len(updates) != 3 || updates[1]["completed"] != int64(50) || updates[1]["total"] != int64(200) || updates[2]["status"] != "success" {
		t.Fatalf("%v", updates)
	}
	// Without the container's key, the router refuses.
	if answer := manager.Handle(Request{Op: "status", Kind: "llamacpp", Port: fake.port()}, nil); answer["state"].(map[string]any)["online"] != false {
		t.Fatalf("%v", answer)
	}
}

func TestLlamaPullSaysWhyADownloadFailed(t *testing.T) {
	fake := router(t, "", `{"model":"ggml-org/new-GGUF:Q4_K_M","event":"download_failed","data":{"error":"repo not found"}}`)
	answer := (&Manager{Machine: on}).Handle(Request{Op: "pull", Kind: "llamacpp", Port: fake.port(), Model: "ggml-org/new-GGUF:Q4_K_M"}, nil)
	if answer["ok"] != false || answer["error"] != "repo not found" {
		t.Fatalf("%v", answer)
	}
	for _, bad := range []string{"not-a-repo", "a/b/c", "a/b:Q4?x=1", "../x"} {
		if answer := (&Manager{Machine: on}).Handle(Request{Op: "pull", Kind: "llamacpp", Port: fake.port(), Model: bad}, nil); answer["ok"] != false {
			t.Fatalf("%s was pulled", bad)
		}
	}
}

func TestLlamaDeleteLoadUnload(t *testing.T) {
	fake := router(t, "", "")
	manager := &Manager{Machine: on}
	if answer := manager.Handle(Request{Op: "delete", Kind: "llamacpp", Port: fake.port(), Model: "ggml-org/qwen-GGUF:Q8_0"}, nil); answer["ok"] != true {
		t.Fatalf("%v", answer)
	}
	if answer := manager.Handle(Request{Op: "load", Kind: "llamacpp", Port: fake.port(), Model: "ggml-org/qwen-GGUF:Q8_0"}, nil); answer["ok"] != true || fake.bodies["POST /models/load"]["model"] != "ggml-org/qwen-GGUF:Q8_0" {
		t.Fatalf("%v", answer)
	}
	if answer := manager.Handle(Request{Op: "unload", Kind: "llamacpp", Port: fake.port(), Model: "x/y"}, nil); answer["ok"] != false || !strings.Contains(answer["error"].(string), "not loaded") {
		t.Fatalf("a refused unload looked fine: %v", answer)
	}
	if answer := (&Manager{Machine: off}).Handle(Request{Op: "delete", Kind: "llamacpp", Port: fake.port(), Model: "x/y"}, nil); answer["ok"] != false {
		t.Fatalf("changed models with servers off: %v", answer)
	}
}

func TestLlamaLogIsAgentMachines(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	manager := &Manager{Machine: off}
	if answer := manager.Handle(Request{Op: "log", Kind: "llamacpp", Port: 8080}, nil); answer["ok"] != false {
		t.Fatalf("%v", answer)
	}
	_ = os.WriteFile(filepath.Join(os.Getenv("AI_PROXY_MACHINE_HOME"), "llamacpp.log"), []byte("main: server is listening"), 0o600)
	if answer := manager.Handle(Request{Op: "log", Kind: "llamacpp", Port: LlamaPort}, nil); answer["text"] != "main: server is listening" {
		t.Fatalf("%v", answer)
	}
}
