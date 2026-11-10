package presence

import (
	"encoding/json"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"ai-control-mesh/machine/internal/providers"
	"ai-control-mesh/machine/internal/settings"
)

// Provider is a model server running on this machine, which the server can add as a backend.
type Provider struct {
	Type    string `json:"type"`
	Port    int    `json:"port"`
	Version string `json:"version,omitempty"`
	Models  *int   `json:"models,omitempty"`
	// Flavor says which OpenAI-compatible server an "api" provider is (lmstudio, vllm, sglang,
	// or openai for a port named in AI_PROXY_API_PORTS).
	Flavor string `json:"flavor,omitempty"`
	// APIKey is a Strata profile's key, for the server to forward agents' requests with.
	// The server keeps it in memory only; it's never logged here.
	APIKey string `json:"api_key,omitempty"`
}

// Where each kind of model server listens by default.
var probes = []struct {
	kind, path string
	port       int
}{
	{"ollama", "/api/version", 11434},
	{"docker", "/engines/v1/models", 12434},
	{"strata", "/v1/models", 18500},
	// llama-server's router lists every model it can serve here, loaded or not (its port: providers.LlamaServerPort).
	{"llamacpp", "/models", 0},
}

// Where the OpenAI-compatible servers that only serve (their models are managed in the server
// itself) listen by default. They're reported as type "api"; only their /v1/models is read.
var apiProbes = []struct {
	flavor string
	port   int
}{
	{"lmstudio", 1234},
	{"vllm", 8000},
	{"sglang", 30000},
}

// APIPortsEnvironment names more ports with an OpenAI-compatible server, comma separated.
const APIPortsEnvironment = "AI_PROXY_API_PORTS"

// apiPorts are the ports named in AI_PROXY_API_PORTS; entries that aren't ports are ignored.
func apiPorts() []int {
	var ports []int
	for _, entry := range strings.Split(os.Getenv(APIPortsEnvironment), ",") {
		port, err := strconv.Atoi(strings.TrimSpace(entry))
		if err == nil && port > 0 && port <= 65535 {
			ports = append(ports, port)
		}
	}
	return ports
}

// withStrata adds the Strata profiles serving on another port than Strata's default, as
// the servers' state reports them (a profile's own config picks its port).
func withStrata(found []Provider, serving map[string]any) []Provider {
	strata, _ := serving["strata"].(map[string]any)
	profiles, _ := strata["profiles"].([]any)
	for _, item := range profiles {
		profile, _ := item.(map[string]any)
		address, _ := profile["url"].(string)
		parsed, err := url.Parse(address)
		if err != nil || !settings.IsLoopbackURL(address) {
			continue
		}
		port, err := strconv.Atoi(parsed.Port())
		if err != nil || port <= 0 {
			continue
		}
		known := false
		for _, provider := range found {
			known = known || provider.Port == port
		}
		if !known {
			found = append(found, Provider{Type: "strata", Port: port})
		}
	}
	return found
}

// withKeys adds the key of the Strata profile serving on each Strata provider's port.
func withKeys(found []Provider) []Provider {
	var instances []providers.Instance
	for i := range found {
		if found[i].Type != "strata" {
			continue
		}
		if instances == nil {
			instances = providers.Discover()
		}
		for _, instance := range instances {
			if instance.Port() == found[i].Port && instance.APIKey != "" && settings.IsLoopbackURL(instance.URL) {
				found[i].APIKey = instance.APIKey
				break
			}
		}
	}
	return found
}

// DetectProviders asks each default port on loopback whether its model server answers, then
// the OpenAI-compatible servers' ports (apiProbes and AI_PROXY_API_PORTS) not already taken.
func DetectProviders() []Provider {
	client := settings.Client(1500 * time.Millisecond)
	found := []Provider{}
	for _, probe := range probes {
		if probe.kind == "llamacpp" {
			probe.port = providers.LlamaServerPort()
		}
		response, err := client.Get("http://127.0.0.1:" + strconv.Itoa(probe.port) + probe.path)
		if err != nil {
			continue
		}
		var body map[string]any
		_ = json.NewDecoder(response.Body).Decode(&body)
		response.Body.Close()
		// Strata answers 401 without its key; it's still there.
		if response.StatusCode != http.StatusOK && !(probe.kind == "strata" && response.StatusCode == http.StatusUnauthorized) {
			continue
		}
		provider := Provider{Type: probe.kind, Port: probe.port}
		if version, ok := body["version"].(string); ok {
			provider.Version = version
		}
		if models, ok := body["data"].([]any); ok {
			count := len(models)
			provider.Models = &count
		}
		if probe.kind == "ollama" {
			provider.Models = ollamaModels(client, probe.port)
		}
		found = append(found, provider)
	}
	type candidate struct {
		flavor string
		port   int
	}
	candidates := []candidate{}
	for _, probe := range apiProbes {
		candidates = append(candidates, candidate{probe.flavor, probe.port})
	}
	for _, port := range apiPorts() {
		candidates = append(candidates, candidate{"openai", port})
	}
	for _, probe := range candidates {
		if reported(found, probe.port) {
			continue
		}
		if count, ok := openAIModels(client, probe.port); ok {
			found = append(found, Provider{Type: "api", Port: probe.port, Flavor: probe.flavor, Models: &count})
		}
	}
	return found
}

func reported(found []Provider, port int) bool {
	for _, provider := range found {
		if provider.Port == port {
			return true
		}
	}
	return false
}

// openAIModels counts the models an OpenAI-compatible server lists. Many unrelated programs
// listen on these ports (8000 above all), so only a 200 with an OpenAI model list counts:
// {"data": [...]}, each item an object with an id.
func openAIModels(client *http.Client, port int) (int, bool) {
	response, err := client.Get("http://127.0.0.1:" + strconv.Itoa(port) + "/v1/models")
	if err != nil {
		return 0, false
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return 0, false
	}
	var body map[string]any
	if json.NewDecoder(response.Body).Decode(&body) != nil {
		return 0, false
	}
	models, ok := body["data"].([]any)
	if !ok {
		return 0, false
	}
	for _, item := range models {
		model, _ := item.(map[string]any)
		if id, _ := model["id"].(string); id == "" {
			return 0, false
		}
	}
	return len(models), true
}

func ollamaModels(client *http.Client, port int) *int {
	response, err := client.Get("http://127.0.0.1:" + strconv.Itoa(port) + "/api/tags")
	if err != nil {
		return nil
	}
	defer response.Body.Close()
	var tags struct {
		Models []any `json:"models"`
	}
	if json.NewDecoder(response.Body).Decode(&tags) != nil {
		return nil
	}
	count := len(tags.Models)
	return &count
}
