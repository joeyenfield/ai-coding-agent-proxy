package servers

// The settings agent-machine starts llama.cpp's llama-server with, kept in llamacpp.json in
// its folder and edited from the console: llama-server reads every option from a
// LLAMA_ARG_* variable as well as from its flag. Where it listens isn't one of them
// (agent-machine always puts it on loopback, where tunnels reach it), and neither is
// anything that would let a model run commands here: llama-server's built-in tools, its
// MCP client and its agent mode are refused, as are an API key and a presets file.

import (
	"path/filepath"
	"reflect"
	"strings"

	"ai-coding-agent-proxy/machine/internal/providers"
	"ai-coding-agent-proxy/machine/internal/settings"
)

const (
	groupModels    = "Models"
	groupReasoning = "Reasoning"
)

var LlamaGroups = []string{groupMemory, groupSched, groupHardware, groupReasoning, groupModels, groupServer}

func onOff(s Setting) Setting {
	s.False, s.True = "off", "on"
	return s
}

// LlamaSettings is the table the console shows, in order.
var LlamaSettings = []Setting{
	setting("ctx_size", groupMemory, "Context length (tokens)", "int", int64(16384),
		"8192 = small tasks, 16384 = recommended, 32768 = larger tasks, 65536 = large agent sessions. 0 = the model's own (with Fit on, the largest that fits).",
		"LLAMA_ARG_CTX_SIZE").bounds(0, 1048576),
	setting("cache_type_k", groupMemory, "KV cache type (K)", "choice", "f16",
		"f16 = full precision, most memory. q8_0 ≈ half. q4_0 ≈ a quarter, with some loss.",
		"LLAMA_ARG_CACHE_TYPE_K").choices(false, "f16", "bf16", "q8_0", "q5_1", "q5_0", "q4_1", "q4_0", "iq4_nl"),
	setting("cache_type_v", groupMemory, "KV cache type (V)", "choice", "f16",
		"A quantized V cache needs Flash Attention.",
		"LLAMA_ARG_CACHE_TYPE_V").choices(false, "f16", "bf16", "q8_0", "q5_1", "q5_0", "q4_1", "q4_0", "iq4_nl"),
	setting("flash_attn", groupMemory, "Flash Attention", "choice", "auto", "auto = where the backend supports it.", "LLAMA_ARG_FLASH_ATTN").choices(false, "auto", "on", "off"),
	onOff(setting("fit", groupMemory, "Fit to memory", "bool", true,
		"Pick the context and GPU layers that aren't set so the model fits in this machine's memory.", "LLAMA_ARG_FIT")),
	setting("cache_ram", groupMemory, "Prompt cache (MiB)", "int", int64(8192),
		"RAM for reusing earlier prompts. 0 = off, -1 = no limit.", "LLAMA_ARG_CACHE_RAM").bounds(-1, 1048576),

	setting("parallel", groupSched, "Parallel requests", "int", int64(1),
		"Slots per model. 1 = lowest KV cache memory; -1 = automatic.", "LLAMA_ARG_N_PARALLEL").bounds(-1, 128),
	setting("models_max", groupSched, "Max loaded models", "int", int64(1),
		"Models loaded at once; the least recently used is unloaded first. 0 = no limit.", "LLAMA_ARG_MODELS_MAX").bounds(0, 128),
	setting("models_autoload", groupSched, "Load models on request", "bool", true,
		"Load a model when an agent asks for it. Off: only models loaded from the console answer.", "LLAMA_ARG_MODELS_AUTOLOAD"),

	setting("gpu_layers", groupHardware, "GPU layers", "choice", "auto",
		"Layers to put on the GPU: auto, all, or a number. 0 = CPU only.", "LLAMA_ARG_N_GPU_LAYERS").choices(true, "auto", "all", "0"),
	setting("device", groupHardware, "Devices", "str", "",
		`Which devices to use, e.g. "CUDA0" or "Vulkan0,Vulkan1" ("none" = CPU only). Empty = all. The log lists them when llama-server starts.`, "LLAMA_ARG_DEVICE"),
	setting("threads", groupHardware, "CPU threads", "int", int64(-1), "-1 = automatic.", "LLAMA_ARG_THREADS").bounds(-1, 1024),

	setting("reasoning_budget", groupReasoning, "Thinking budget (tokens)", "int", int64(-1),
		"How much a thinking model may think before it answers. -1 = no limit, 0 = don't think. Small models on slow machines answer far sooner with a budget.",
		"LLAMA_ARG_THINK_BUDGET").bounds(-1, 1048576),
	setting("jinja", groupReasoning, "Model's chat template", "bool", true,
		"Use the chat template in the model (needed for tool calls).", "LLAMA_ARG_JINJA"),

	setting("cache_dir", groupModels, "Download folder", "str", "",
		"Where models downloaded from Hugging Face go. Empty = llama.cpp's standard cache.", "LLAMA_CACHE"),
	setting("models_dir", groupModels, "GGUF folder", "str", "",
		"A folder of .gguf files to serve as well (a subfolder for a model in several files). Empty = llamacpp-models in agent-machine's data folder (AI_PROXY_MACHINE_DATA, else its own folder). Models agent-machine downloads itself go here.", "LLAMA_ARG_MODELS_DIR"),

	onOff(setting("webui", groupServer, "Web UI", "bool", false, "llama-server's own chat page, on this computer only.", "LLAMA_ARG_UI")),
	setting("metrics", groupServer, "Metrics endpoint", "bool", false, "Prometheus metrics at /metrics.", "LLAMA_ARG_ENDPOINT_METRICS"),
}

// llamaReserved is what agent-machine sets itself, or the server must never set.
var llamaReserved = []string{"LLAMA_ARG_HOST", "LLAMA_ARG_PORT", "LLAMA_API_KEY", "LLAMA_ARG_API_KEY", "LLAMA_ARG_API_KEY_FILE", "LLAMA_ARG_MODELS_PRESET", "LLAMA_ARG_REUSE_PORT", "LLAMA_ARG_CORS_ORIGINS"}

// llamaForbidden are parts of names that switch on tools, MCP or agent mode: ways for a model to run things here.
var llamaForbidden = []string{"TOOL", "MCP", "AGENT", "SSL", "PROPS"}

var llamaTable = table{
	settings: LlamaSettings, groups: LlamaGroups, file: "llamacpp.json",
	extra: func(name string) string {
		if !strings.HasPrefix(name, "LLAMA_ARG_") && !strings.HasPrefix(name, "GGML_") {
			return "only LLAMA_ARG_* and GGML_* variables can be set here"
		}
		if contains(llamaReserved, name) {
			return "agent-machine sets where llama-server listens, and it takes no API key or presets file here"
		}
		for _, part := range llamaForbidden {
			if strings.Contains(name, part) {
				return "llama-server's tools, MCP servers, agent mode and server options can't be turned on from the console"
			}
		}
		return ""
	},
}

// LlamaDefaultConfig is every setting at its default, and no extra variables.
func LlamaDefaultConfig() map[string]any { return llamaTable.defaults() }

// LlamaValidateConfig checks and tidies a config from the console against the table.
func LlamaValidateConfig(data map[string]any) (map[string]any, error) {
	return llamaTable.validate(data)
}

// LlamaManagedEnv is the variables the config sets; nil means unset.
func LlamaManagedEnv(config map[string]any) map[string]*string {
	env := llamaTable.managed(config)
	if env["LLAMA_ARG_MODELS_DIR"] == nil {
		// Always a GGUF folder: where agent-machine downloads models when the router can't.
		folder := defaultModelsDir()
		env["LLAMA_ARG_MODELS_DIR"] = &folder
	}
	return env
}

// defaultModelsDir is the GGUF folder when none is set: llamacpp-models in agent-machine's data
// folder (its own folder, unless AI_PROXY_MACHINE_DATA moves it).
func defaultModelsDir() string { return filepath.Join(settings.Data(), "llamacpp-models") }

// llamaModelsDir is the GGUF folder agent-machine's llama-server serves.
func llamaModelsDir() string {
	if folder := LlamaManagedEnv(LoadLlamaConfig())["LLAMA_ARG_MODELS_DIR"]; folder != nil {
		return *folder
	}
	return defaultModelsDir()
}

func init() { providers.LlamaFolder = llamaModelsDir }

// LlamaEnvironment is the environment llama-server starts with: base without what the
// console mustn't set, with the managed variables applied.
func LlamaEnvironment(base []string, config map[string]any) []string {
	env := llamaTable.environment(base, config, func(name string) bool {
		upper := strings.ToUpper(name)
		if contains(llamaReserved, upper) {
			return true
		}
		if strings.HasPrefix(upper, "LLAMA_ARG_") {
			for _, part := range llamaForbidden {
				if strings.Contains(upper, part) {
					return true
				}
			}
		}
		return false
	})
	if llamaTable.managed(config)["LLAMA_ARG_MODELS_DIR"] == nil {
		env = append(env, "LLAMA_ARG_MODELS_DIR="+defaultModelsDir())
	}
	return env
}

// LlamaWarnings is what the console warns about in a config.
func LlamaWarnings(config map[string]any) []string {
	warnings := []string{}
	if context, _ := asInt(config["ctx_size"]); context != 0 && context < 8192 {
		warnings = append(warnings, "Context below 8K is likely too small for coding agents.")
	} else if context != 0 && context < 16384 {
		warnings = append(warnings, "Some coding agents may exhaust this context rapidly.")
	}
	if parallel, _ := asInt(config["parallel"]); parallel > 1 {
		warnings = append(warnings, "Parallel requests increase KV cache memory requirements.")
	}
	if config["cache_type_v"] != "f16" && config["cache_type_v"] != "bf16" && config["flash_attn"] == "off" {
		warnings = append(warnings, "A quantized V cache needs Flash Attention; set it to auto or on.")
	}
	return warnings
}

// LoadLlamaConfig is the saved config over the defaults; a missing or broken file is the defaults.
func LoadLlamaConfig() map[string]any { return llamaTable.load() }

// SaveLlamaConfig validates a config and saves it.
func SaveLlamaConfig(data map[string]any) (map[string]any, error) { return llamaTable.save(data) }

// llamaSettings is the "llamacpp_settings" answer: the table, the saved config, and whether
// the llama-server agent-machine runs was started with it.
func (m *Manager) llamaSettings() map[string]any {
	config := LoadLlamaConfig()
	env := LlamaManagedEnv(config)
	var matches any
	if pid := llamaPID(); pid > 0 && m.llamaRunning() {
		if applied := loadState().LlamaEnv; applied != nil {
			matches = reflect.DeepEqual(applied, publicEnv(env))
		}
	}
	return map[string]any{
		"schema":                map[string]any{"groups": LlamaGroups, "settings": LlamaSettings},
		"config":                config,
		"env":                   publicEnv(env),
		"warnings":              LlamaWarnings(config),
		"applied_matches_saved": matches,
		"config_path":           llamaTable.path(),
		"log_path":              llamaLog(),
	}
}
