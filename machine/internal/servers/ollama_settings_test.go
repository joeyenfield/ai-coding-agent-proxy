package servers

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

func TestOllamaSettingsAreCheckedAndSaved(t *testing.T) {
	home := t.TempDir()
	t.Setenv("AI_PROXY_MACHINE_HOME", home)
	manager := &Manager{Machine: on, Port: freePort(t)}
	answer := manager.Handle(Request{ID: "r1", Op: "settings", Kind: "ollama"})
	current := answer["ollama_settings"].(map[string]any)
	if answer["ok"] != true || current["config"].(map[string]any)["kv_cache_type"] != "q4_0" || current["applied_matches_saved"] != nil {
		t.Fatalf("%v", answer)
	}
	for _, s := range current["schema"].(map[string]any)["settings"].([]Setting) {
		if s.Key == "host" {
			t.Fatal("OLLAMA_HOST is agent-machine's, not a setting")
		}
	}
	for _, bad := range []map[string]any{
		{"context_length": 10},
		{"kv_cache_type": "q2"},
		{"num_parallel": "many"},
		{"extra_env": "LD_PRELOAD=/tmp/x.so"},
		{"extra_env": "OLLAMA_HOST=0.0.0.0:1"},
		{"extra_env": "not a line"},
	} {
		if answer := manager.Handle(Request{ID: "r2", Op: "configure", Kind: "ollama", Config: bad}); answer["ok"] != false {
			t.Fatalf("%v accepted: %v", bad, answer)
		}
	}
	answer = manager.Handle(Request{ID: "r3", Op: "configure", Kind: "ollama", Config: map[string]any{
		"context_length": 32768.0, "flash_attention": false, "keep_alive": "2h", "gpus": "0", "extra_env": "# note\nOLLAMA_FOO=bar\nOLLAMA_BAZ=",
	}})
	saved := answer["ollama_settings"].(map[string]any)
	env := saved["env"].(map[string]any)
	if answer["ok"] != true || env["OLLAMA_CONTEXT_LENGTH"] != "32768" || env["OLLAMA_FLASH_ATTENTION"] != "0" || env["CUDA_VISIBLE_DEVICES"] != "0" || env["OLLAMA_FOO"] != "bar" || env["OLLAMA_BAZ"] != nil || env["OLLAMA_MODELS"] != nil {
		t.Fatalf("%v", answer)
	}
	if warnings := saved["warnings"].([]string); len(warnings) != 1 || !strings.Contains(warnings[0], "Flash Attention") {
		t.Fatalf("%v", warnings)
	}
	data, err := os.ReadFile(ollamaConfigPath())
	var file map[string]any
	if err != nil || json.Unmarshal(data, &file) != nil || file["keep_alive"] != "2h" {
		t.Fatalf("%s %v", data, err)
	}
	if LoadConfig()["context_length"] != int64(32768) {
		t.Fatalf("%v", LoadConfig())
	}
}

func TestEnvironmentAppliesManagedVariables(t *testing.T) {
	config := DefaultConfig()
	config["models_dir"] = "/models"
	config["extra_env"] = "OLLAMA_DEBUG="
	env := Environment([]string{"PATH=/bin", "OLLAMA_MODELS=/old", "OLLAMA_HOST=1.2.3.4:5", "OLLAMA_DEBUG=1", "CUDA_VISIBLE_DEVICES=1"}, config)
	joined := "\n" + strings.Join(env, "\n") + "\n"
	for _, want := range []string{"\nPATH=/bin\n", "\nOLLAMA_MODELS=/models\n", "\nOLLAMA_KV_CACHE_TYPE=q4_0\n", "\nOLLAMA_GPU_OVERHEAD=0\n"} {
		if !strings.Contains(joined, want) {
			t.Fatalf("missing %q in %v", want, env)
		}
	}
	for _, gone := range []string{"OLLAMA_HOST=", "OLLAMA_DEBUG=", "CUDA_VISIBLE_DEVICES=", "/old"} {
		if strings.Contains(joined, gone) {
			t.Fatalf("%q left in %v", gone, env)
		}
	}
}

func TestOllamaStartsWithItsSettingsAndRestarts(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	setCommand(t, OllamaEnv, fake(t, "fake_ollama.py"))
	manager := &Manager{Machine: on, Port: freePort(t)}
	if _, err := SaveConfig(map[string]any{"num_parallel": 2}); err != nil {
		t.Fatal(err)
	}
	if answer := manager.Handle(Request{ID: "r1", Op: "start", Kind: "ollama"}); answer["ok"] != true {
		t.Fatalf("%v", answer)
	}
	defer manager.Handle(Request{ID: "r9", Op: "stop", Kind: "ollama"})
	log, _ := os.ReadFile(ollamaLog())
	if !strings.Contains(string(log), "OLLAMA_NUM_PARALLEL=2") {
		t.Fatalf("log: %s", log)
	}
	if matches := manager.Handle(Request{ID: "r2", Op: "settings", Kind: "ollama"})["ollama_settings"].(map[string]any)["applied_matches_saved"]; matches != true {
		t.Fatalf("applied: %v", matches)
	}
	manager.Handle(Request{ID: "r3", Op: "configure", Kind: "ollama", Config: map[string]any{"num_parallel": 3}})
	if matches := manager.Handle(Request{ID: "r4", Op: "settings", Kind: "ollama"})["ollama_settings"].(map[string]any)["applied_matches_saved"]; matches != false {
		t.Fatalf("after a change: %v", matches)
	}
	pid := loadState().OllamaPID
	if answer := manager.Handle(Request{ID: "r5", Op: "restart", Kind: "ollama"}); answer["ok"] != true || loadState().OllamaPID == pid {
		t.Fatalf("restart: %v", answer)
	}
	if matches := manager.Handle(Request{ID: "r6", Op: "settings", Kind: "ollama"})["ollama_settings"].(map[string]any)["applied_matches_saved"]; matches != true {
		t.Fatalf("after restart: %v", matches)
	}
}
