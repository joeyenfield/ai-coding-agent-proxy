package servers

import (
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"ai-coding-agent-proxy/machine/internal/process"
	"ai-coding-agent-proxy/machine/internal/settings"
)

// The fakes live with the server's tests, which use them too.
func fake(t *testing.T, name string) []string {
	t.Helper()
	python, err := exec.LookPath("python")
	if err != nil {
		if python, err = exec.LookPath("python3"); err != nil {
			t.Skip("Python isn't installed")
		}
	}
	script, _ := filepath.Abs(filepath.Join("..", "..", "..", "server", "tests", name))
	return []string{python, script}
}

func setCommand(t *testing.T, env string, command []string) {
	t.Helper()
	encoded, _ := json.Marshal(command)
	t.Setenv(env, string(encoded))
}

func freePort(t *testing.T) int {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	return listener.Addr().(*net.TCPAddr).Port
}

func on() *settings.Machine { return &settings.Machine{Servers: true} }

func TestEverythingIsRefusedUntilTheMachineAllowsIt(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	manager := &Manager{Machine: func() *settings.Machine { return &settings.Machine{} }}
	answer := manager.Handle(Request{ID: "r1", Op: "start", Kind: "ollama"})
	if answer["ok"] != false || answer["type"] != "server_result" || answer["request"] != "r1" || !strings.Contains(answer["error"].(string), "servers on") {
		t.Fatalf("%v", answer)
	}
	if described := manager.Describe(); described["enabled"] != false || len(described) != 1 {
		t.Fatalf("off, but described %v", described)
	}
}

func TestOllamaStartsServesAndStops(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	setCommand(t, OllamaEnv, fake(t, "fake_ollama.py"))
	manager := &Manager{Machine: on, Port: freePort(t)}
	if state := manager.Describe()["ollama"].(map[string]any); state["found"] != true || state["running"] != false {
		t.Fatalf("before: %v", state)
	}
	if answer := manager.Handle(Request{ID: "r1", Op: "start", Kind: "ollama", Bind: "0.0.0.0:1; evil"}); answer["ok"] != false {
		t.Fatalf("a bind that isn't 127.0.0.1 or 0.0.0.0 was accepted: %v", answer)
	}
	answer := manager.Handle(Request{ID: "r2", Op: "start", Kind: "ollama", Bind: "127.0.0.1"})
	if answer["ok"] != true {
		t.Fatalf("start: %v", answer)
	}
	state := answer["servers"].(map[string]any)["ollama"].(map[string]any)
	if state["running"] != true || state["managed"] != true || state["version"] != "0.0.0-fake" || state["bind"] != "127.0.0.1" {
		t.Fatalf("after start: %v", state)
	}
	pid := loadState().OllamaPID
	// Starting it again is fine: it's already serving.
	if answer := manager.Handle(Request{ID: "r3", Op: "start", Kind: "ollama"}); answer["ok"] != true || loadState().OllamaPID != pid {
		t.Fatalf("second start: %v", answer)
	}
	if answer := manager.Handle(Request{ID: "r4", Op: "stop", Kind: "ollama"}); answer["ok"] != true {
		t.Fatalf("stop: %v", answer)
	}
	if process.Alive(pid) || manager.ollamaVersion() != "" {
		t.Fatal("Ollama still runs after stop")
	}
}

func TestAnOllamaItDidntStartIsLeftAlone(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	command := fake(t, "fake_ollama.py")
	port := freePort(t)
	other := exec.Command(command[0], command[1], "serve")
	other.Env = append(os.Environ(), "OLLAMA_HOST=127.0.0.1:"+itoa(port))
	if err := other.Start(); err != nil {
		t.Fatal(err)
	}
	defer other.Process.Kill()
	manager := &Manager{Machine: on, Port: port}
	for i := 0; i < 100 && manager.ollamaVersion() == ""; i++ {
		waitABit()
	}
	answer := manager.Handle(Request{ID: "r1", Op: "stop", Kind: "ollama"})
	if answer["ok"] != false || !strings.Contains(answer["error"].(string), "can't stop it") {
		t.Fatalf("%v", answer)
	}
	if state := answer["servers"].(map[string]any)["ollama"].(map[string]any); state["running"] != true || state["managed"] != false {
		t.Fatalf("%v", state)
	}
}

func TestAnOllamaThatFailsToStartSaysWhy(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	t.Setenv("FAKE_OLLAMA_FAIL", "1")
	setCommand(t, OllamaEnv, fake(t, "fake_ollama.py"))
	answer := (&Manager{Machine: on, Port: freePort(t)}).Handle(Request{ID: "r1", Op: "start", Kind: "ollama"})
	if answer["ok"] != false || !strings.Contains(answer["error"].(string), "address already in use") {
		t.Fatalf("%v", answer)
	}
}

func TestStrataProfilesStartAndStopThroughStrata(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	stateFile := filepath.Join(t.TempDir(), "strata.json")
	t.Setenv("FAKE_STRATA_STATE", stateFile)
	if err := os.WriteFile(stateFile, []byte(`{"profiles": {"app-1": "stopped", "coder": "stopped"}}`), 0o600); err != nil {
		t.Fatal(err)
	}
	setCommand(t, StrataEnv, fake(t, "fake_strata.py"))
	manager := &Manager{Machine: on, Port: freePort(t)}
	strata := manager.Describe()["strata"].(map[string]any)
	if strata["found"] != true || len(strata["profiles"].([]any)) != 2 {
		t.Fatalf("%v", strata)
	}
	answer := manager.Handle(Request{ID: "r1", Op: "start", Kind: "strata", Profile: "coder"})
	if answer["ok"] != true {
		t.Fatalf("start: %v", answer)
	}
	var started map[string]any
	for _, item := range answer["servers"].(map[string]any)["strata"].(map[string]any)["profiles"].([]any) {
		if item.(map[string]any)["id"] == "coder" {
			started = item.(map[string]any)
		}
	}
	if started["state"] != "ready" || started["url"] != "http://127.0.0.1:18777" {
		t.Fatalf("after start: %v", started)
	}
	for _, bad := range []string{"missing", "../app-1", "app-1 --plan", ""} {
		if answer := manager.Handle(Request{ID: "r2", Op: "start", Kind: "strata", Profile: bad}); answer["ok"] != false {
			t.Fatalf("profile %q accepted", bad)
		}
	}
	if answer := manager.Handle(Request{ID: "r3", Op: "stop", Kind: "strata", Profile: "coder"}); answer["ok"] != true {
		t.Fatalf("stop: %v", answer)
	}
	data, _ := os.ReadFile(stateFile)
	var seen struct {
		Profiles map[string]string `json:"profiles"`
		Calls    [][]string        `json:"calls"`
	}
	if err := json.Unmarshal(data, &seen); err != nil {
		t.Fatal(err)
	}
	calls := map[string]bool{}
	for _, call := range seen.Calls {
		calls[strings.Join(call, " ")] = true
	}
	if seen.Profiles["coder"] != "stopped" || !calls["runtime start --profile coder"] || !calls["runtime stop --profile coder"] {
		t.Fatalf("strata saw: %s", data)
	}
}

// Every model installed in Strata gets a profile of its own once servers are on, and a profile
// whose run config has gone (its model removed or reinstalled in Strata) is reported, not started.
func TestStrataProfilesFollowTheInstalledModels(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	dir := t.TempDir()
	stateFile := filepath.Join(dir, "strata.json")
	t.Setenv("FAKE_STRATA_STATE", stateFile)
	config := func(name string) string {
		path := filepath.Join(dir, name+".json")
		if err := os.WriteFile(path, []byte("{}"), 0o600); err != nil {
			t.Fatal(err)
		}
		return path
	}
	iq3, coder, partial := config("strata-iq3_s"), config("strata-coder"), config("strata-q2")
	gone := filepath.Join(dir, "native-instances", "b4bfef02.json")
	state := map[string]any{
		// app-1 names a run config that's gone; "iq3_s" is taken by a profile of another model.
		"profiles": map[string]string{"app-1": "stopped", "iq3_s": "stopped"},
		"configs":  map[string]string{"app-1": gone, "iq3_s": coder},
		"models": []map[string]any{
			{"id": "iq3_s", "name": "qwen3.8-flash-next-iq3_s", "source_config": iq3, "component_status": "present", "missing": []any{}},
			{"id": "coder", "name": "qwen-coder", "source_config": coder, "component_status": "present", "missing": []any{}},
			{"id": "q2", "name": "half-downloaded", "source_config": partial, "component_status": "missing", "missing": []any{"model"}},
		},
	}
	data, _ := json.Marshal(state)
	if err := os.WriteFile(stateFile, data, 0o600); err != nil {
		t.Fatal(err)
	}
	setCommand(t, StrataEnv, fake(t, "fake_strata.py"))
	manager := &Manager{Machine: on, Port: freePort(t)}

	byID := func(describe map[string]any) map[string]map[string]any {
		found := map[string]map[string]any{}
		for _, item := range describe["strata"].(map[string]any)["profiles"].([]any) {
			found[item.(map[string]any)["id"].(string)] = item.(map[string]any)
		}
		return found
	}
	profiles := byID(manager.Describe())
	// iq3_s's own model got a new profile, numbered because the name was taken; coder already had one.
	if len(profiles) != 3 || profiles["iq3_s-2"]["model"] != "qwen3.8-flash-next-iq3_s" || profiles["iq3_s"]["model"] != "qwen-coder" {
		t.Fatalf("%v", profiles)
	}
	if profiles["app-1"]["state"] != "broken" || !strings.Contains(fmt.Sprint(profiles["app-1"]["broken"]), "run config is gone") {
		t.Fatalf("app-1: %v", profiles["app-1"])
	}
	if _, ok := profiles["q2"]; ok {
		t.Fatal("a model missing its files got a profile")
	}
	// Asking again adds nothing: the new profile already uses that run config.
	if again := byID(manager.Describe()); len(again) != 3 {
		t.Fatalf("%v", again)
	}
	answer := manager.Handle(Request{ID: "r1", Op: "start", Kind: "strata", Profile: "app-1"})
	if answer["ok"] != false || !strings.Contains(answer["error"].(string), "run config is gone") {
		t.Fatalf("start app-1: %v", answer)
	}
	if answer := manager.Handle(Request{ID: "r2", Op: "start", Kind: "strata", Profile: "iq3_s-2"}); answer["ok"] != true {
		t.Fatalf("start iq3_s-2: %v", answer)
	}
	data, _ = os.ReadFile(stateFile)
	var seen struct {
		Calls [][]string `json:"calls"`
	}
	if err := json.Unmarshal(data, &seen); err != nil {
		t.Fatal(err)
	}
	created := 0
	for _, call := range seen.Calls {
		if len(call) > 1 && call[0] == "profile" && call[1] == "create" {
			created++
			if strings.Join(call, " ") != "profile create iq3_s-2 --from-config "+iq3 {
				t.Fatalf("created %v", call)
			}
		}
		if strings.Join(call, " ") == "runtime start --profile app-1" {
			t.Fatal("a broken profile was started")
		}
	}
	if created != 1 {
		t.Fatalf("created %d profiles: %s", created, data)
	}
}

// A Strata from before `models list --installed` keeps working with its saved profiles.
func TestStrataWithoutModelsListUsesItsProfiles(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	stateFile := filepath.Join(t.TempDir(), "strata.json")
	t.Setenv("FAKE_STRATA_STATE", stateFile)
	if err := os.WriteFile(stateFile, []byte(`{"profiles": {"coder": "stopped"}, "no_models_list": true}`), 0o600); err != nil {
		t.Fatal(err)
	}
	setCommand(t, StrataEnv, fake(t, "fake_strata.py"))
	manager := &Manager{Machine: on, Port: freePort(t)}
	strata := manager.Describe()["strata"].(map[string]any)
	if strata["error"] != nil || len(strata["profiles"].([]any)) != 1 {
		t.Fatalf("%v", strata)
	}
	if answer := manager.Handle(Request{ID: "r1", Op: "start", Kind: "strata", Profile: "coder"}); answer["ok"] != true {
		t.Fatalf("%v", answer)
	}
}

// With servers off, nothing is asked of Strata, so no profile is made.
func TestStrataIsLeftAloneWhileServersAreOff(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	stateFile := filepath.Join(t.TempDir(), "strata.json")
	t.Setenv("FAKE_STRATA_STATE", stateFile)
	setCommand(t, StrataEnv, fake(t, "fake_strata.py"))
	manager := &Manager{Machine: func() *settings.Machine { return &settings.Machine{} }, Port: freePort(t)}
	if describe := manager.Describe(); describe["enabled"] != false {
		t.Fatalf("%v", describe)
	}
	if _, err := os.Stat(stateFile); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("strata was run while servers are off")
	}
}

func itoa(value int) string { return strconv.Itoa(value) }

func waitABit() { time.Sleep(50 * time.Millisecond) }
