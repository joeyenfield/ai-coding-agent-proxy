// Package containers runs model servers in containers on this machine, for the server.
//
// The server asks over the machine's fleet connection (start, stop, restart, remove,
// status, logs); the machine runs the `docker` CLI (or `podman`) itself. `docker run` is
// root on this machine, so containers are off until `agent-machine containers on`, and
// every request is checked here whatever the server allowed: only the engines below,
// each engine's own settings, plain image names, GPU lists and a volume name or folder.
// The engine table lives here, and the machine tells the server about it in its hello.
package containers

// Option is one setting an engine's image reads from the environment.
type Option struct {
	Env     string   `json:"env"`
	Label   string   `json:"label"`
	Choices []string `json:"choices"`
	Hint    string   `json:"hint"`
}

// Engine is a kind of model server this machine can run in a container.
type Engine struct {
	ID          string   `json:"id"`
	Label       string   `json:"label"`
	BackendType string   `json:"backend_type"`
	Image       string   `json:"image"`
	Images      []string `json:"images"`
	Port        int      `json:"port"`
	DataPath    string   `json:"data_path"`
	Volume      string   `json:"volume"`
	HealthPath  string   `json:"health_path"`
	StopTimeout int      `json:"stop_timeout"`
	Flags       []string `json:"-"`
	Options     []Option `json:"options"`
	Summary     string   `json:"summary"`
}

const strataImage = "ghcr.io/niko1221/strata"

// Engines in the order the console offers them.
var Engines = []Engine{
	{
		ID: "strata", Label: "Strata", BackendType: "strata",
		Image:  strataImage + ":latest-cuda13",
		Images: images(strataImage+":latest-", "cuda13", "cuda12", "rocm-gfx120x", "rocm-gfx110x", "rocm-gfx1151", "sycl"),
		Port:   8080, DataPath: "/data", Volume: "strata-data", HealthPath: "/health",
		// Strata's Docker guide: a 2-minute stop lets it unload cleanly, memlock pins the model's RAM.
		StopTimeout: 120, Flags: []string{"--ulimit", "memlock=-1:-1", "--shm-size", "2g"},
		Options: []Option{
			{"MODEL", "Model size", []string{"IQ2_XS", "IQ3_XXS", "IQ3_S", "Q2_0", "IQ1_M", "UD-IQ4_XS", "UD-Q4_K_XL", "UD-Q5_K_XL", "UD-Q6_K_XL", "Q8_0"}, "Strata picks one for the card"},
			{"FAMILY", "Family", []string{"qwen", "unsloth", "gyro", "coder", "swift"}, "Strata's recommendation"},
			{"CONTEXT", "Context", []string{"32k", "64k", "128k", "256k"}, "The largest that fits"},
			{"KV", "KV cache", []string{"int8", "q4_0", "k8v4", "fp16"}, "int8"},
			{"VISION", "Vision", []string{"off", "on", "gpu", "cpu"}, "off"},
			{"PARALLEL", "Parallel requests", []string{"1", "2", "3", "4", "5", "6", "7", "8"}, "Fitted to the card"},
		},
		Summary: "Strata's own image. The first start downloads the model into the shared volume (tens of GB), later starts take a minute or two to load it.",
	},
	{
		ID: "ollama", Label: "Ollama", BackendType: "ollama",
		Image: "ollama/ollama:latest", Images: []string{"ollama/ollama:latest", "ollama/ollama:rocm"},
		Port: 11434, DataPath: "/root/.ollama", Volume: "ollama-models", HealthPath: "/api/version",
		StopTimeout: 30, Flags: []string{},
		Options: []Option{
			{"OLLAMA_CONTEXT_LENGTH", "Context", []string{"8192", "16384", "32768", "65536", "131072"}, "Ollama's default"},
			{"OLLAMA_KV_CACHE_TYPE", "KV cache", []string{"f16", "q8_0", "q4_0"}, "f16"},
			{"OLLAMA_FLASH_ATTENTION", "Flash attention", []string{"1", "0"}, "Off"},
			{"OLLAMA_NUM_PARALLEL", "Parallel requests", []string{"1", "2", "4"}, "Ollama's default"},
			{"OLLAMA_KEEP_ALIVE", "Keep a model loaded", []string{"5m", "30m", "-1"}, "5m"},
		},
		Summary: "The official Ollama image. Models pulled into the shared volume are there for every Ollama container.",
	},
}

func images(prefix string, flavors ...string) []string {
	result := make([]string, len(flavors))
	for index, flavor := range flavors {
		result[index] = prefix + flavor
	}
	return result
}

// EngineByID is the engine with that id, or nil.
func EngineByID(id string) *Engine {
	for index := range Engines {
		if Engines[index].ID == id {
			return &Engines[index]
		}
	}
	return nil
}

// stopTimeout is how long a stop waits for the engine to unload.
func stopTimeout(engine string) int {
	if found := EngineByID(engine); found != nil {
		return found.StopTimeout
	}
	return 30
}
