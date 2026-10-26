package servers

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

// An Ollama that's a systemd service is started, stopped and restarted through it once
// `sudo agent-machine servers service` allowed it, with the console's settings in its
// environment file; until then it's refused with that command.
func TestASystemdOllamaRestartsThroughItsServiceWithItsSettings(t *testing.T) {
	home := t.TempDir()
	t.Setenv("AI_PROXY_MACHINE_HOME", home)
	fakeCtl := fake(t, "fake_systemctl.py")
	setCommand(t, SystemctlEnv, fakeCtl)
	setCommand(t, ServiceCtlEnv, fakeCtl)
	envFile := filepath.Join(home, "ollama.env")
	t.Setenv(ServiceEnvEnv, envFile)
	t.Setenv("FAKE_SERVICE_STATE", filepath.Join(home, "service.json"))
	port := freePort(t)
	manager := &Manager{Machine: on, Port: port}
	t.Cleanup(func() { manager.Handle(Request{ID: "cleanup", Op: "stop", Kind: "ollama"}) })

	// Not set up yet: there's no environment file it may write.
	answer := manager.Handle(Request{ID: "r1", Op: "restart", Kind: "ollama"})
	if answer["ok"] != false || !strings.Contains(answer["error"].(string), "sudo agent-machine servers service") {
		t.Fatalf("before setting it up: %v", answer)
	}
	ollama := manager.Describe()["ollama"].(map[string]any)
	if ollama["service"] != "systemd" || ollama["service_allowed"] != false || ollama["managed"] != false {
		t.Fatalf("described before setup: %v", ollama)
	}

	// After `sudo agent-machine servers service`: the file exists and this user may write it.
	if err := os.WriteFile(envFile, []byte("# set up\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := SaveConfig(map[string]any{"num_parallel": 2}); err != nil {
		t.Fatal(err)
	}
	answer = manager.Handle(Request{ID: "r2", Op: "restart", Kind: "ollama", Bind: "127.0.0.1"})
	if answer["ok"] != true {
		t.Fatalf("restart through the service: %v", answer)
	}
	written, _ := os.ReadFile(envFile)
	if !strings.Contains(string(written), `OLLAMA_NUM_PARALLEL="2"`) || !strings.Contains(string(written), `OLLAMA_HOST="127.0.0.1:`+strconv.Itoa(port)+`"`) {
		t.Fatalf("the service's environment:\n%s", written)
	}
	if manager.ollamaVersion() == "" {
		t.Fatal("the service's Ollama isn't answering")
	}
	ollama = manager.Describe()["ollama"].(map[string]any)
	if ollama["managed"] != true || ollama["service_allowed"] != true || ollama["running"] != true {
		t.Fatalf("described after a restart: %v", ollama)
	}
	if manager.ollamaSettings()["applied_matches_saved"] != true {
		t.Fatal("it runs the saved settings, but the console isn't told so")
	}
	// The service really started with them.
	var service struct {
		Env map[string]string `json:"env"`
	}
	data, _ := os.ReadFile(filepath.Join(home, "service.json"))
	if json.Unmarshal(data, &service) != nil || service.Env["OLLAMA_NUM_PARALLEL"] != "2" {
		t.Fatalf("the service's process: %s", data)
	}

	answer = manager.Handle(Request{ID: "r3", Op: "stop", Kind: "ollama"})
	if answer["ok"] != true {
		t.Fatalf("stop through the service: %v", answer)
	}
	for deadline := time.Now().Add(5 * time.Second); time.Now().Before(deadline) && manager.ollamaVersion() != ""; time.Sleep(100 * time.Millisecond) {
	}
	if manager.ollamaVersion() != "" {
		t.Fatal("still answering after the service stopped")
	}
}

func TestEnvironmentValuesAreQuotedForSystemd(t *testing.T) {
	if got := quoteEnv(`a "b" \c`); got != `"a \"b\" \\c"` {
		t.Fatalf("quoted: %s", got)
	}
}
