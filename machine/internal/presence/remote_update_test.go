package presence

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	"ai-coding-agent-proxy/machine/internal/settings"
)

func TestARemoteUpdateRunsOnlyWhenTheMachineTakesThem(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"server_version": "9.9.9", "version": "9.9.9", "exact": true, "files": []any{}})
	}))
	defer server.Close()
	var spawned [][]string
	presence := &Presence{SpawnUpdate: func(args []string) error {
		spawned = append(spawned, args)
		return nil
	}}

	machine := settings.LoadMachine()
	answer := presence.RemoteUpdate(server.URL, UpdateRequest{ID: "u1"})
	if answer["ok"] != false || !strings.Contains(answer["error"].(string), "updates on") || answer["request"] != "u1" || len(spawned) != 0 {
		t.Fatalf("updates off: %v, spawned %v", answer, spawned)
	}

	machine.Updates = true
	if err := machine.Save(); err != nil {
		t.Fatal(err)
	}
	answer = presence.RemoteUpdate(server.URL, UpdateRequest{ID: "u2", Agent: true})
	if answer["ok"] != true || answer["updating"] != true || answer["to"] != "9.9.9" || answer["from"] != settings.Version {
		t.Fatalf("updates on: %v", answer)
	}
	if want := [][]string{{"update", "--server", server.URL, "--with-agent"}}; !reflect.DeepEqual(spawned, want) {
		t.Fatalf("spawned %v", spawned)
	}
}
