package providers

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"

	"ai-coding-agent-proxy/machine/internal/settings"
)

// OllamaPort is where the Ollama agent-machine starts listens (package servers).
const OllamaPort = 11434

// ollamaStatus is the Ollama's models, from /api/tags, with what /api/ps says is loaded.
func ollamaStatus(base string) map[string]any {
	var (
		wait                   sync.WaitGroup
		tags, running, version any
		tagsStatus             int
		tagsErr                error
	)
	wait.Add(3)
	go func() { defer wait.Done(); tags, tagsStatus, tagsErr = getJSON(base+"/api/tags", nil, 5*time.Second) }()
	go func() { defer wait.Done(); running, _, _ = getJSON(base+"/api/ps", nil, 5*time.Second) }()
	go func() { defer wait.Done(); version, _, _ = getJSON(base+"/api/version", nil, 5*time.Second) }()
	wait.Wait()
	if tagsErr != nil {
		return map[string]any{"online": false, "error": tagsErr.Error(), "models": []any{}}
	}
	if tagsStatus >= 400 {
		return map[string]any{"online": false, "error": fmt.Sprintf("Ollama returned %d", tagsStatus), "models": []any{}}
	}
	loaded := map[string]map[string]any{}
	for _, item := range list(object(running)["models"]) {
		entry := object(item)
		loaded[str(entry["name"])] = entry
	}
	models := []map[string]any{}
	for _, item := range list(object(tags)["models"]) {
		entry := object(item)
		details := object(entry["details"])
		name := str(entry["name"])
		model := map[string]any{
			"name": name, "size": entry["size"], "modified_at": entry["modified_at"],
			"family": details["family"], "parameter_size": details["parameter_size"], "quantization": details["quantization_level"],
			"loaded": false, "context_length": nil, "size_loaded": nil, "size_vram": nil, "gpu_percent": nil, "expires_at": nil,
		}
		if state, ok := loaded[name]; ok {
			size, vram := number(state["size"]), number(state["size_vram"])
			model["loaded"], model["context_length"], model["size_loaded"], model["size_vram"], model["expires_at"] = true, state["context_length"], state["size"], state["size_vram"], state["expires_at"]
			if size > 0 {
				model["gpu_percent"] = math.Round(vram * 100 / size)
			}
		}
		models = append(models, model)
	}
	sort.Slice(models, func(i, j int) bool { return str(models[i]["name"]) < str(models[j]["name"]) })
	return map[string]any{"online": true, "version": object(version)["version"], "models": models}
}

// ollamaPull relays /api/pull's progress lines as they come.
func ollamaPull(base, model string, progress Progress) error {
	response, err := send(http.MethodPost, base+"/api/pull", map[string]any{"model": model, "stream": true}, nil, 0)
	if err != nil {
		return unavailable("Ollama", err)
	}
	defer response.Body.Close()
	if response.StatusCode >= 400 {
		data, _ := io.ReadAll(io.LimitReader(response.Body, 64<<10))
		return errors.New(errorText(data, response.StatusCode, "Ollama"))
	}
	var failed error
	err = streamLines(response.Body, func(line []byte) {
		var update map[string]any
		if json.Unmarshal(line, &update) != nil {
			return
		}
		if text := str(update["error"]); text != "" {
			failed = errors.New(text)
			update["status"] = "error"
		}
		progress(update)
	})
	if failed != nil {
		return failed
	}
	return err
}

func ollamaCall(method, url string, body any, timeout time.Duration) error {
	response, err := send(method, url, body, nil, timeout)
	if err != nil {
		return unavailable("Ollama", err)
	}
	defer response.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if response.StatusCode >= 400 {
		return errors.New(errorText(data, response.StatusCode, "Ollama"))
	}
	return nil
}

// ollamaLog is the log of the Ollama agent-machine started (package servers writes it).
func ollamaLog(port, tail int) (string, error) {
	if port != OllamaPort {
		return "", errors.New("Only the Ollama agent-machine starts (on port 11434) has a log here")
	}
	text, err := tailFile(filepath.Join(settings.Home(), "ollama.log"), tail)
	if errors.Is(err, os.ErrNotExist) {
		return "No log yet: agent-machine writes one when it starts Ollama.", nil
	}
	return text, err
}

// tailFile is the end of a file, at most tail bytes (200 kB by default, 2 MB at most).
func tailFile(path string, tail int) (string, error) {
	if tail <= 0 {
		tail = 200_000
	}
	tail = max(1024, min(tail, 2_000_000))
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return "", err
	}
	if offset := info.Size() - int64(tail); offset > 0 {
		if _, err := file.Seek(offset, io.SeekStart); err != nil {
			return "", err
		}
	}
	data, err := io.ReadAll(file)
	return string(data), err
}
