package providers

import (
	"net/http"
	"reflect"
	"strings"
	"testing"
)

func TestAPIStatusListsTheModelsWithTheirContext(t *testing.T) {
	fake := serve(t, map[string]http.HandlerFunc{"GET /v1/models": reply(map[string]any{"object": "list", "data": []any{
		map[string]any{"id": "qwen3-coder", "owned_by": "vllm", "max_model_len": 32768},
		map[string]any{"id": "gemma", "owned_by": "organization_owner", "loaded_context_length": 8192, "context_length": 4096},
		map[string]any{"id": "plain"},
		map[string]any{"owned_by": "nobody"},
	}})})
	answer := (&Manager{Machine: off}).Handle(Request{ID: "r1", Op: "status", Kind: "api", Port: fake.port()}, nil)
	if answer["ok"] != true || answer["request"] != "r1" {
		t.Fatalf("%v", answer)
	}
	want := map[string]any{"online": true, "version": "", "models": []map[string]any{
		{"id": "qwen3-coder", "owned_by": "vllm", "context_length": 32768.0},
		{"id": "gemma", "owned_by": "organization_owner", "context_length": 4096.0},
		{"id": "plain", "owned_by": ""},
	}}
	if state := answer["state"]; !reflect.DeepEqual(state, want) {
		t.Fatalf("%v", state)
	}
}

func TestAPIStatusOfflineOrNotAModelList(t *testing.T) {
	gone := serve(t, nil)
	port := gone.port()
	gone.Close()
	other := serve(t, map[string]http.HandlerFunc{"GET /v1/models": reply(map[string]any{"status": "ok"})})
	failing := serve(t, nil)
	for _, port := range []int{port, other.port(), failing.port()} {
		state := (&Manager{}).Handle(Request{Op: "status", Kind: "api", Port: port}, nil)["state"].(map[string]any)
		if state["online"] != false || state["error"] == "" {
			t.Fatalf("%v", state)
		}
	}
}

func TestAPIModelsAreManagedInTheServerItself(t *testing.T) {
	fake := serve(t, nil)
	for _, op := range []string{"pull", "delete", "load", "unload", "log", "files", "reload"} {
		answer := (&Manager{Machine: on}).Handle(Request{Op: op, Kind: "api", Port: fake.port(), Model: "m"}, nil)
		if answer["ok"] != false || !strings.Contains(answer["error"].(string), "managed in that server itself") {
			t.Fatalf("%s: %v", op, answer)
		}
	}
	// With servers off, changing ops are refused by the gate first, as for every kind.
	answer := (&Manager{Machine: off}).Handle(Request{Op: "pull", Kind: "api", Port: fake.port(), Model: "m"}, nil)
	if answer["ok"] != false || !strings.Contains(answer["error"].(string), "servers on") {
		t.Fatalf("%v", answer)
	}
	if len(fake.calls) != 0 {
		t.Fatalf("the server was called: %v", fake.calls)
	}
}
