package presence

import (
	"encoding/json"
	"net/http"
	"net/url"
	"strconv"
	"time"

	"ai-coding-agent-proxy/machine/internal/providers"
	"ai-coding-agent-proxy/machine/internal/settings"
)

// Provider is a model server running on this machine, which the server can add as a backend.
type Provider struct {
	Type    string `json:"type"`
	Port    int    `json:"port"`
	Version string `json:"version,omitempty"`
	Models  *int   `json:"models,omitempty"`
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

// DetectProviders asks each default port on loopback whether its model server answers.
func DetectProviders() []Provider {
	client := settings.Client(1500 * time.Millisecond)
	found := []Provider{}
	for _, probe := range probes {
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
	return found
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
