package servers

// The OLLAMA_* settings agent-machine starts Ollama with, kept in ollama.json in its
// folder and edited from the console. OLLAMA_HOST isn't one of them: agent-machine sets
// it from where the server needs Ollama to listen. Extra variables may only be OLLAMA_*
// ones, so the server can't set what else a program here runs with.

import (
	"path/filepath"
	"reflect"
	"strings"

	"ai-control-mesh/machine/internal/settings"
)

const (
	groupMemory   = "Memory / KV cache"
	groupSched    = "Concurrency / scheduling"
	groupHardware = "Hardware"
	groupServer   = "Server"
)

var Groups = []string{groupMemory, groupSched, groupHardware, groupServer}

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

	setting("models_dir", groupServer, "Models directory", "str", "", "Empty = standard model directory (ollama-models in AI_PROXY_MACHINE_DATA when that's set).", "OLLAMA_MODELS"),
	setting("origins", groupServer, "CORS origins", "str", "", "Comma-separated extra allowed origins.", "OLLAMA_ORIGINS"),
	setting("debug", groupServer, "Debug logging", "bool", false, "Verbose server log.", "OLLAMA_DEBUG"),
}

var ollamaTable = table{
	settings: Settings, groups: Groups, file: "ollama.json",
	extra: func(name string) string {
		if !strings.HasPrefix(name, "OLLAMA_") || name == "OLLAMA_HOST" {
			return "only OLLAMA_* variables (other than OLLAMA_HOST) can be set here"
		}
		return ""
	},
}

// DefaultConfig is every setting at its default, and no extra variables.
func DefaultConfig() map[string]any { return ollamaTable.defaults() }

// ParseExtraEnv reads KEY=VALUE lines; a blank value unsets the variable, '#' starts a comment.
func ParseExtraEnv(text string) (map[string]*string, error) { return ollamaTable.parseExtra(text) }

// ValidateConfig checks and tidies a config from the console against the table.
func ValidateConfig(data map[string]any) (map[string]any, error) { return ollamaTable.validate(data) }

// ManagedEnv is the variables the config sets; nil means unset.
func ManagedEnv(config map[string]any) map[string]*string { return ollamaTable.managed(config) }

// Environment is the environment Ollama starts with: base with the managed variables applied.
func Environment(base []string, config map[string]any) []string {
	env := ollamaTable.environment(base, config, func(name string) bool { return strings.EqualFold(name, "OLLAMA_HOST") })
	if folder, ok := ollamaDataModels(config); ok {
		env = append(env, "OLLAMA_MODELS="+folder)
	}
	return env
}

// ollamaDataModels is where an Ollama agent-machine starts keeps its models when
// AI_PROXY_MACHINE_DATA is set and its settings name no folder: ollama-models there. Without
// that variable Ollama keeps its own default (~/.ollama/models), so models already pulled stay.
func ollamaDataModels(config map[string]any) (string, bool) {
	data, ok := settings.DataSet()
	if !ok || ollamaTable.managed(config)["OLLAMA_MODELS"] != nil {
		return "", false
	}
	return filepath.Join(data, "ollama-models"), true
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

func ollamaConfigPath() string { return ollamaTable.path() }

// LoadConfig is the saved config over the defaults; a missing or broken file is the defaults.
func LoadConfig() map[string]any { return ollamaTable.load() }

// SaveConfig validates a config and saves it.
func SaveConfig(data map[string]any) (map[string]any, error) { return ollamaTable.save(data) }

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
