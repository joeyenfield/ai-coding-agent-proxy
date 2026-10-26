package servers

// The OLLAMA_* settings agent-machine starts Ollama with, kept in ollama.json in its
// folder and edited from the console. OLLAMA_HOST isn't one of them: agent-machine sets
// it from where the server needs Ollama to listen. Extra variables may only be OLLAMA_*
// ones, so the server can't set what else a program here runs with.

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"

	"ai-coding-agent-proxy/machine/internal/settings"
)

// Setting is one OLLAMA_* option.
type Setting struct {
	Key      string   `json:"key"`
	Group    string   `json:"group"`
	Label    string   `json:"label"`
	Kind     string   `json:"kind"` // bool, int, str or choice
	Default  any      `json:"default"`
	Help     string   `json:"help"`
	Env      []string `json:"env"`
	Choices  []string `json:"choices"`
	Editable bool     `json:"editable"` // a choice that also takes free text
	Minimum  int64    `json:"minimum"`
	Maximum  int64    `json:"maximum"`
	// Scale multiplies an int when it's written to the environment.
	Scale int64 `json:"-"`
}

const (
	groupMemory   = "Memory / KV cache"
	groupSched    = "Concurrency / scheduling"
	groupHardware = "Hardware"
	groupServer   = "Server"
	maxInt        = 1<<31 - 1
)

var Groups = []string{groupMemory, groupSched, groupHardware, groupServer}

func setting(key, group, label, kind string, def any, help string, env ...string) Setting {
	return Setting{Key: key, Group: group, Label: label, Kind: kind, Default: def, Help: help, Env: env, Choices: []string{}, Maximum: maxInt, Scale: 1}
}

func (s Setting) choices(editable bool, values ...string) Setting {
	s.Choices, s.Editable = values, editable
	return s
}

func (s Setting) bounds(minimum, maximum int64) Setting {
	s.Minimum, s.Maximum = minimum, maximum
	return s
}

// Settings is the table the console shows, in order.
var Settings = []Setting{
	setting("kv_cache_type", groupMemory, "KV cache type", "choice", "q4_0",
		"f16 = full precision, most memory. q8_0 ≈ 53% of f16. q4_0 ≈ 28% of f16. Quantized types require Flash Attention.",
		"OLLAMA_KV_CACHE_TYPE").choices(false, "f16", "q8_0", "q4_0"),
	setting("flash_attention", groupMemory, "Flash Attention", "bool", true, "Required for a quantized KV cache.", "OLLAMA_FLASH_ATTENTION"),
	setting("context_length", groupMemory, "Context length (tokens)", "int", int64(16384),
		"8192 = small tasks, 16384 = recommended, 32768 = larger tasks, 65536 = large agent sessions.",
		"OLLAMA_CONTEXT_LENGTH").bounds(1024, 1048576),
	func() Setting {
		s := setting("gpu_overhead_mb", groupMemory, "GPU overhead (MB)", "int", int64(0), "VRAM to reserve for the OS / other apps. 0 = no reservation.", "OLLAMA_GPU_OVERHEAD").bounds(0, 1048576)
		s.Scale = 1024 * 1024
		return s
	}(),

	setting("num_parallel", groupSched, "Parallel requests", "int", int64(1), "1 = lowest KV cache memory. Each extra slot multiplies KV cache use.", "OLLAMA_NUM_PARALLEL").bounds(1, 128),
	setting("max_loaded_models", groupSched, "Max loaded models", "int", int64(1), "Maximum number of models resident at once.", "OLLAMA_MAX_LOADED_MODELS").bounds(1, 128),
	setting("max_queue", groupSched, "Max queued requests", "int", int64(512), "Requests beyond this are rejected while busy.", "OLLAMA_MAX_QUEUE").bounds(1, 100000),
	setting("keep_alive", groupSched, "Keep alive", "choice", "-1", "-1 = keep loaded forever, 5m = unload after 5 min idle, 0 = unload immediately.", "OLLAMA_KEEP_ALIVE").choices(true, "-1", "0", "5m", "30m", "1h", "24h"),
	setting("load_timeout", groupSched, "Load timeout", "choice", "10m", "How long to wait for a model to load before giving up.", "OLLAMA_LOAD_TIMEOUT").choices(true, "5m", "10m", "20m", "30m"),
	setting("sched_spread", groupSched, "Spread across GPUs", "bool", false, "Always split models across all GPUs.", "OLLAMA_SCHED_SPREAD"),
	setting("multiuser_cache", groupSched, "Multi-user cache", "bool", false, "Optimise prompt caching for multiple concurrent users.", "OLLAMA_MULTIUSER_CACHE"),
	setting("noprune", groupSched, "Don't prune blobs", "bool", false, "Skip pruning unused model blobs on startup.", "OLLAMA_NOPRUNE"),

	setting("gpus", groupHardware, "GPU IDs", "str", "", `e.g. "0" or "0,1". Empty = all GPUs. "-1" = CPU only.`, "CUDA_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "GGML_VK_VISIBLE_DEVICES"),
	setting("llm_library", groupHardware, "LLM library", "choice", "", "Force a backend. Empty = automatic.", "OLLAMA_LLM_LIBRARY").choices(true, "", "cuda_v12", "cuda_v13", "rocm", "vulkan", "cpu"),
	setting("vulkan", groupHardware, "Vulkan backend", "bool", false, "Experimental Vulkan backend.", "OLLAMA_VULKAN"),
	setting("new_engine", groupHardware, "New engine", "bool", false, "Opt into Ollama's new engine where supported.", "OLLAMA_NEW_ENGINE"),

	setting("models_dir", groupServer, "Models directory", "str", "", "Empty = standard model directory.", "OLLAMA_MODELS"),
	setting("origins", groupServer, "CORS origins", "str", "", "Comma-separated extra allowed origins.", "OLLAMA_ORIGINS"),
	setting("debug", groupServer, "Debug logging", "bool", false, "Verbose server log.", "OLLAMA_DEBUG"),
}

// DefaultConfig is every setting at its default, and no extra variables.
func DefaultConfig() map[string]any {
	config := map[string]any{"extra_env": ""}
	for _, s := range Settings {
		config[s.Key] = s.Default
	}
	return config
}

// ParseExtraEnv reads KEY=VALUE lines; a blank value unsets the variable, '#' starts a comment.
func ParseExtraEnv(text string) (map[string]*string, error) {
	result := map[string]*string{}
	for number, raw := range strings.Split(text, "\n") {
		line := strings.TrimSpace(raw)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		name, value, found := strings.Cut(line, "=")
		name = strings.TrimSpace(name)
		if !found || name == "" || strings.ContainsAny(name, " \t") {
			return nil, fmt.Errorf("Extra environment line %d is not KEY=VALUE: %q", number+1, strings.TrimRight(raw, "\r"))
		}
		if !strings.HasPrefix(name, "OLLAMA_") || name == "OLLAMA_HOST" {
			return nil, fmt.Errorf("Extra environment line %d sets %s: only OLLAMA_* variables (other than OLLAMA_HOST) can be set here", number+1, name)
		}
		if value = strings.TrimSpace(value); value == "" {
			result[name] = nil
		} else {
			result[name] = &value
		}
	}
	return result, nil
}

func asInt(value any) (int64, bool) {
	switch typed := value.(type) {
	case float64:
		if typed != float64(int64(typed)) {
			return 0, false
		}
		return int64(typed), true
	case int64:
		return typed, true
	case int:
		return int64(typed), true
	case string:
		parsed, err := strconv.ParseInt(strings.TrimSpace(typed), 10, 64)
		return parsed, err == nil
	case bool:
		if typed {
			return 1, true
		}
		return 0, true
	}
	return 0, false
}

func asText(value any) string {
	switch typed := value.(type) {
	case nil:
		return ""
	case string:
		return typed
	case float64:
		return strconv.FormatFloat(typed, 'f', -1, 64)
	}
	return fmt.Sprint(value)
}

func truthy(value any) bool {
	switch typed := value.(type) {
	case bool:
		return typed
	case string:
		return typed != ""
	case nil:
		return false
	}
	number, ok := asInt(value)
	return !ok || number != 0
}

// ValidateConfig checks and tidies a config from the console against the table.
func ValidateConfig(data map[string]any) (map[string]any, error) {
	if data == nil {
		return nil, errors.New("Configuration must be an object")
	}
	config := DefaultConfig()
	for _, s := range Settings {
		value, present := data[s.Key]
		if !present {
			continue
		}
		switch s.Kind {
		case "bool":
			config[s.Key] = truthy(value)
		case "int":
			number, ok := asInt(value)
			if !ok {
				return nil, fmt.Errorf("%s must be a number", s.Label)
			}
			if number < s.Minimum || number > s.Maximum {
				return nil, fmt.Errorf("%s must be between %d and %d", s.Label, s.Minimum, s.Maximum)
			}
			config[s.Key] = number
		case "choice":
			text := strings.TrimSpace(asText(value))
			if !s.Editable && len(s.Choices) > 0 && !contains(s.Choices, text) {
				return nil, fmt.Errorf("%s must be one of: %s", s.Label, strings.Join(s.Choices, ", "))
			}
			config[s.Key] = text
		default:
			config[s.Key] = strings.TrimSpace(asText(value))
		}
	}
	config["extra_env"] = asText(data["extra_env"])
	// A bad line is refused on saving rather than on starting.
	if _, err := ParseExtraEnv(config["extra_env"].(string)); err != nil {
		return nil, err
	}
	return config, nil
}

func contains(values []string, value string) bool {
	for _, item := range values {
		if item == value {
			return true
		}
	}
	return false
}

// ManagedEnv is the variables the config sets; nil means unset.
func ManagedEnv(config map[string]any) map[string]*string {
	env := map[string]*string{}
	for _, s := range Settings {
		value, present := config[s.Key]
		if !present {
			value = s.Default
		}
		var text *string
		switch s.Kind {
		case "bool":
			flag := "0"
			if truthy(value) {
				flag = "1"
			}
			text = &flag
		case "int":
			number, _ := asInt(value)
			formatted := strconv.FormatInt(number*s.Scale, 10)
			text = &formatted
		default:
			if trimmed := strings.TrimSpace(asText(value)); trimmed != "" {
				text = &trimmed
			}
		}
		for _, name := range s.Env {
			env[name] = text
		}
	}
	extra, _ := ParseExtraEnv(asText(config["extra_env"]))
	for name, value := range extra {
		env[name] = value
	}
	return env
}

// publicEnv is ManagedEnv as JSON: a string, or null for unset.
func publicEnv(env map[string]*string) map[string]any {
	public := map[string]any{}
	for name, value := range env {
		if value == nil {
			public[name] = nil
		} else {
			public[name] = *value
		}
	}
	return public
}

// Environment is the environment Ollama starts with: base with the managed variables applied.
func Environment(base []string, config map[string]any) []string {
	managed := ManagedEnv(config)
	result := []string{}
	for _, entry := range base {
		name, _, _ := strings.Cut(entry, "=")
		if _, ours := managed[name]; ours || strings.EqualFold(name, "OLLAMA_HOST") {
			continue
		}
		result = append(result, entry)
	}
	names := make([]string, 0, len(managed))
	for name := range managed {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		if value := managed[name]; value != nil {
			result = append(result, name+"="+*value)
		}
	}
	return result
}

// ConfigWarnings is what the console warns about in a config.
func ConfigWarnings(config map[string]any) []string {
	warnings := []string{}
	context, _ := asInt(config["context_length"])
	if context < 8192 {
		warnings = append(warnings, "Context below 8K is likely too small for coding agents.")
	} else if context < 16384 {
		warnings = append(warnings, "Some coding agents may exhaust this context rapidly.")
	}
	if parallel, _ := asInt(config["num_parallel"]); parallel > 1 {
		warnings = append(warnings, "Parallel requests increase KV cache memory requirements.")
	}
	if config["kv_cache_type"] != "f16" && !truthy(config["flash_attention"]) {
		warnings = append(warnings, "A quantized KV cache requires Flash Attention; enable it.")
	}
	return warnings
}

func ollamaConfigPath() string { return filepath.Join(settings.Home(), "ollama.json") }

// LoadConfig is the saved config over the defaults; a missing or broken file is the defaults.
func LoadConfig() map[string]any {
	config := DefaultConfig()
	data, err := os.ReadFile(ollamaConfigPath())
	if err != nil {
		return config
	}
	var saved map[string]any
	if json.Unmarshal(data, &saved) != nil {
		return config
	}
	if valid, err := ValidateConfig(saved); err == nil {
		return valid
	}
	return config
}

// SaveConfig validates a config and saves it.
func SaveConfig(data map[string]any) (map[string]any, error) {
	config, err := ValidateConfig(data)
	if err != nil {
		return nil, err
	}
	encoded, _ := json.MarshalIndent(config, "", "  ")
	if err := os.MkdirAll(settings.Home(), 0o700); err != nil {
		return nil, err
	}
	temporary := ollamaConfigPath() + ".tmp"
	if err := os.WriteFile(temporary, append(encoded, '\n'), 0o600); err != nil {
		return nil, err
	}
	return config, os.Rename(temporary, ollamaConfigPath())
}

// ollamaSettings is the "ollama_settings" answer: the table, the saved config, and
// whether the Ollama agent-machine runs was started with it.
func (m *Manager) ollamaSettings() map[string]any {
	config := LoadConfig()
	env := ManagedEnv(config)
	var matches any
	if (managedPID() > 0 || loadState().Service) && m.ollamaVersion() != "" {
		if applied := loadState().OllamaEnv; applied != nil {
			matches = reflect.DeepEqual(applied, publicEnv(env))
		}
	}
	return map[string]any{
		"schema":                map[string]any{"groups": Groups, "settings": Settings},
		"config":                config,
		"env":                   publicEnv(env),
		"warnings":              ConfigWarnings(config),
		"applied_matches_saved": matches,
		"config_path":           ollamaConfigPath(),
		"log_path":              ollamaLog(),
	}
}
