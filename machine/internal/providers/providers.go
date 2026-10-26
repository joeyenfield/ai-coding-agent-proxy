// Package providers answers the server's questions about the model servers running on
// this machine ({"type": "provider"} over the fleet connection): their state, their
// models (pull, delete, load, unload), their logs, this machine's load, and the model
// catalogues (ollama.com, Hugging Face, Docker Hub) to pull from.
//
// The server never talks to a model server to manage it; it asks the machine that runs
// it. Requests only name a kind, a loopback port and a model, and each op calls fixed
// paths on 127.0.0.1:<port>, so the server can't reach anything else through here.
// Reading is always allowed; changing models needs `agent-machine servers on`, like
// starting and stopping the model servers (package servers).
package providers

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"ai-coding-agent-proxy/machine/internal/settings"
)

// Request is the server's {"type": "provider", ...}.
type Request struct {
	Type    string `json:"type"`
	ID      string `json:"request"`
	Op      string `json:"op"`
	Kind    string `json:"kind"`
	Port    int    `json:"port"`
	Model   string `json:"model"`
	Key     string `json:"key"`
	Source  string `json:"source"`
	Catalog string `json:"catalog"`
	Query   string `json:"query"`
	Repo    string `json:"repo"`
	Tail    int    `json:"tail"`
}

// Progress hears a pull's progress, as Ollama reports it.
type Progress func(map[string]any)

// Manager answers provider requests.
type Manager struct {
	// Machine reads the machine's settings fresh for each request; tests replace it.
	Machine func() *settings.Machine
	// Host is where the model servers are reached; tests leave it, it's always loopback.
	Host string
	// External makes calls to the catalogues; tests point it at a fake.
	External *http.Client
	// Catalog URLs; tests replace them.
	LibraryURL, HuggingFaceURL, DockerHubURL string

	hub hubCache
}

var changes = map[string]bool{"pull": true, "delete": true, "load": true, "unload": true}

func (m *Manager) machine() *settings.Machine {
	if m.Machine != nil {
		return m.Machine()
	}
	return settings.LoadMachine()
}

func (m *Manager) base(port int) string {
	host := m.Host
	if host == "" {
		host = "127.0.0.1"
	}
	return "http://" + host + ":" + strconv.Itoa(port)
}

// Handle answers one request, always with a provider_result for its id. progress hears a
// pull's progress before the answer.
func (m *Manager) Handle(request Request, progress Progress) map[string]any {
	answer, err := m.handle(request, progress)
	if answer == nil {
		answer = map[string]any{}
	}
	answer["type"], answer["request"] = "provider_result", request.ID
	if err != nil {
		answer["ok"], answer["error"] = false, err.Error()
	} else {
		answer["ok"] = true
	}
	return answer
}

func (m *Manager) handle(request Request, progress Progress) (map[string]any, error) {
	switch request.Op {
	case "metrics":
		return map[string]any{"metrics": Metrics()}, nil
	case "search":
		result, err := m.search(request.Catalog, request.Query, request.Repo)
		return map[string]any{"result": result}, err
	}
	if request.Kind != "ollama" && request.Kind != "strata" && request.Kind != "docker" {
		return nil, fmt.Errorf("unknown model server kind %q", request.Kind)
	}
	if request.Port <= 0 || request.Port > 65535 {
		return nil, errors.New("name the model server's port")
	}
	if changes[request.Op] && !m.machine().Servers {
		return nil, errors.New("Managing model servers is off on this machine (agent-machine servers on)")
	}
	if progress == nil {
		progress = func(map[string]any) {}
	}
	base := m.base(request.Port)
	model := strings.TrimSpace(request.Model)
	if (request.Op == "pull" || request.Op == "delete" || request.Op == "load" || request.Op == "unload") && request.Kind != "strata" && model == "" {
		return nil, errors.New("A model name is required")
	}
	switch request.Kind + " " + request.Op {
	case "ollama status":
		return map[string]any{"state": ollamaStatus(base)}, nil
	case "ollama pull":
		return nil, ollamaPull(base, model, progress)
	case "ollama delete":
		return result("deleted", model), ollamaCall(http.MethodDelete, base+"/api/delete", map[string]any{"model": model}, time.Minute)
	case "ollama load":
		return result("loaded", model), ollamaCall(http.MethodPost, base+"/api/generate", map[string]any{"model": model, "stream": false}, 10*time.Minute)
	case "ollama unload":
		return result("unloaded", model), ollamaCall(http.MethodPost, base+"/api/generate", map[string]any{"model": model, "keep_alive": 0, "stream": false}, time.Minute)
	case "ollama log":
		text, err := ollamaLog(request.Port, request.Tail)
		return map[string]any{"text": text}, err
	case "strata status":
		return map[string]any{"state": strataStatus(base, request.Port, request.Key)}, nil
	case "strata load", "strata unload":
		status, err := strataControl(base, request.Port, request.Key, request.Op)
		return map[string]any{"result": map[string]any{"status": status}}, err
	case "strata log":
		text, err := strataLog(request.Port, request.Source, request.Tail)
		return map[string]any{"text": text}, err
	case "docker status":
		return map[string]any{"state": dockerStatus(base)}, nil
	case "docker pull":
		return nil, dockerPull(base, model, progress)
	case "docker delete":
		_, err := dockerCall(http.MethodDelete, base+"/models/"+modelPath(model), nil, nil, time.Minute)
		return result("deleted", model), err
	case "docker load":
		_, err := dockerCall(http.MethodPost, base+"/engines/v1/chat/completions", map[string]any{"model": model, "messages": []any{}}, map[string]string{"X-Preload-Only": "true"}, 10*time.Minute)
		return result("loaded", model), err
	case "docker unload":
		runners, err := dockerUnload(base, model)
		return map[string]any{"result": map[string]any{"unloaded": model, "runners": runners}}, err
	}
	return nil, fmt.Errorf("%s can't %s", kindLabel(request.Kind), request.Op)
}

func result(key, model string) map[string]any {
	return map[string]any{"result": map[string]any{key: model}}
}

func kindLabel(kind string) string {
	switch kind {
	case "docker":
		return "Docker Model Runner"
	case "strata":
		return "Strata"
	}
	return "Ollama"
}

// HTTP helpers

func send(method, url string, body any, headers map[string]string, timeout time.Duration) (*http.Response, error) {
	var reader io.Reader
	if body != nil {
		data, _ := json.Marshal(body)
		reader = bytes.NewReader(data)
	}
	request, err := http.NewRequest(method, url, reader)
	if err != nil {
		return nil, err
	}
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	for name, value := range headers {
		request.Header.Set(name, value)
	}
	return settings.Client(timeout).Do(request)
}

// getJSON decodes a GET's answer; status is 0 when the server didn't answer.
func getJSON(url string, headers map[string]string, timeout time.Duration) (any, int, error) {
	response, err := send(http.MethodGet, url, nil, headers, timeout)
	if err != nil {
		return nil, 0, err
	}
	defer response.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(response.Body, 32<<20))
	var value any
	if json.Unmarshal(data, &value) != nil {
		value = nil
	}
	return value, response.StatusCode, nil
}

func object(value any) map[string]any {
	if found, ok := value.(map[string]any); ok {
		return found
	}
	return map[string]any{}
}

func list(value any) []any {
	if found, ok := value.([]any); ok {
		return found
	}
	return nil
}

func str(value any) string {
	switch typed := value.(type) {
	case string:
		return typed
	case float64:
		return strconv.FormatFloat(typed, 'f', -1, 64)
	case bool:
		return strconv.FormatBool(typed)
	}
	return ""
}

func number(value any) float64 {
	switch typed := value.(type) {
	case float64:
		return typed
	case int:
		return float64(typed)
	case int64:
		return float64(typed)
	case string:
		parsed, _ := strconv.ParseFloat(typed, 64)
		return parsed
	}
	return 0
}

// errorText is the readable error in a JSON error body, else the body, else the status.
func errorText(data []byte, status int, who string) string {
	var body any
	if json.Unmarshal(data, &body) == nil {
		if found := object(body); len(found) > 0 {
			value := found["error"]
			if value == nil {
				value = found["message"]
			}
			if nested, ok := value.(map[string]any); ok {
				value = nested["message"]
			}
			if text := str(value); text != "" {
				return text
			}
		}
	}
	if text := strings.TrimSpace(string(data)); text != "" {
		return text
	}
	return fmt.Sprintf("%s returned %d", who, status)
}

func unavailable(who string, err error) error {
	return fmt.Errorf("%s unavailable: %v", who, err)
}

// streamLines calls line for each non-empty line of a streamed answer.
func streamLines(body io.Reader, line func([]byte)) error {
	scanner := bufio.NewScanner(body)
	scanner.Buffer(make([]byte, 64*1024), 4<<20)
	for scanner.Scan() {
		if data := bytes.TrimSpace(scanner.Bytes()); len(data) > 0 {
			line(data)
		}
	}
	return scanner.Err()
}
