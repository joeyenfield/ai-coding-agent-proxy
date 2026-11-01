package providers

// llama.cpp's llama-server in router mode (tools/server/README.md, "Using multiple models").
// Routes used:
//
//	GET    /models               every model it can serve: the cache, its models folder; each with its status
//	                             (unloaded, loading, loaded, sleeping, downloading, incomplete)
//	POST   /models               download a Hugging Face model ("user/repo:quant") into the cache, in the background
//	GET    /models/sse           the download's progress (download_progress, then download_finished or download_failed)
//	DELETE /models?model=        delete a cached model
//	POST   /models/load|unload   load or unload one
//	GET    /health               up, and never needs a key
//
// A llama-server run with one model (-m) has no router: it lists that model, loaded, and
// can't change models.

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"ai-coding-agent-proxy/machine/internal/settings"
)

// LlamaPort is where llama-server listens by default (and where agent-machine starts it).
const LlamaPort = 9931

// LlamaPortEnv moves the llama-server agent-machine starts (and looks for) to another port.
const LlamaPortEnv = "AI_PROXY_LLAMACPP_PORT"

// LlamaServerPort is the port agent-machine's llama-server uses: AI_PROXY_LLAMACPP_PORT, else 9931.
func LlamaServerPort() int {
	if port, err := strconv.Atoi(os.Getenv(LlamaPortEnv)); err == nil && port > 0 && port < 65536 {
		return port
	}
	return LlamaPort
}

// llamaModelName is what can be downloaded: a Hugging Face repo, and optionally its quant.
func llamaModelName(model string) error {
	repo, quant, _ := strings.Cut(model, ":")
	parts := strings.Split(repo, "/")
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" || strings.ContainsAny(model, " \t\r\n?#&") || len(model) > 300 {
		return errors.New("name a Hugging Face model as user/repo or user/repo:quant (Q4_K_M, ...)")
	}
	if strings.Contains(quant, "/") {
		return errors.New("a quant is a name such as Q4_K_M")
	}
	return nil
}

func fileSize(path string) any {
	if path == "" {
		return nil
	}
	info, err := os.Stat(path)
	if err != nil {
		return nil
	}
	if !info.IsDir() {
		return info.Size()
	}
	var total int64
	_ = filepath.WalkDir(path, func(file string, entry os.DirEntry, err error) error {
		if err == nil && !entry.IsDir() && strings.HasSuffix(strings.ToLower(entry.Name()), ".gguf") {
			if found, err := entry.Info(); err == nil {
				total += found.Size()
			}
		}
		return nil
	})
	return total
}

// llamaModels is the router's models as the console shows them, loaded first, then by name.
func llamaModels(listed any) []map[string]any {
	models := []map[string]any{}
	for _, raw := range list(object(listed)["data"]) {
		item := object(raw)
		id := str(item["id"])
		if id == "" {
			continue
		}
		status := object(item["status"])
		state := str(status["value"])
		if state == "" {
			// One model, no router: it's what's loaded.
			state = "loaded"
		}
		meta := object(item["meta"])
		path := str(item["path"])
		model := map[string]any{
			"name": id, "state": state, "loaded": state == "loaded" || state == "sleeping",
			"failed": status["failed"] == true, "path": path, "size": fileSize(path),
			"context_train": meta["n_ctx_train"], "parameters": meta["n_params"], "progress": nil,
			"input": object(item["architecture"])["input_modalities"],
		}
		if model["size"] == nil && meta["size"] != nil {
			model["size"] = meta["size"]
		}
		if progress := object(status["progress"]); len(progress) > 0 {
			var done, total float64
			for _, file := range progress {
				done += number(object(file)["done"])
				total += number(object(file)["total"])
			}
			model["progress"] = map[string]any{"completed": int64(done), "total": int64(total)}
		}
		models = append(models, model)
	}
	sort.SliceStable(models, func(i, j int) bool {
		if models[i]["loaded"] != models[j]["loaded"] {
			return models[i]["loaded"] == true
		}
		return str(models[i]["name"]) < str(models[j]["name"])
	})
	return models
}

// llamaHeaders is the key a llama.cpp container takes (the server's, sent with the request), if any.
func llamaHeaders(key string) map[string]string {
	if key == "" {
		return nil
	}
	return map[string]string{"Authorization": "Bearer " + key}
}

func llamaStatus(base, key string) map[string]any {
	listed, status, err := getJSON(base+"/models", llamaHeaders(key), 8*time.Second)
	if err != nil {
		return map[string]any{"online": false, "error": err.Error(), "models": []any{}, "router": false}
	}
	if status >= 400 {
		return map[string]any{"online": false, "error": fmt.Sprintf("llama-server returned %d", status), "models": []any{}, "router": false}
	}
	data := list(object(listed)["data"])
	// The router gives every model a status; a llama-server with one model doesn't.
	router := len(data) == 0
	for _, raw := range data {
		if _, ok := object(raw)["status"]; ok {
			router = true
		}
	}
	return map[string]any{"online": true, "router": router, "models": llamaModels(listed)}
}

func llamaCall(method, address, key string, body any, timeout time.Duration) error {
	response, err := send(method, address, body, llamaHeaders(key), timeout)
	if err != nil {
		return unavailable("llama-server", err)
	}
	defer response.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if response.StatusCode == http.StatusUnauthorized {
		return errors.New("llama-server wants an API key this machine doesn't have")
	}
	if response.StatusCode == http.StatusNotFound {
		return errors.New("this llama-server serves one model: start it in router mode (without -m) to change its models")
	}
	if response.StatusCode >= 400 {
		return errors.New(errorText(data, response.StatusCode, "llama-server"))
	}
	if answer := object(decode(data)); answer["success"] == false {
		return errors.New(errorText(data, response.StatusCode, "llama-server"))
	}
	return nil
}

func decode(data []byte) any {
	var value any
	_ = json.Unmarshal(data, &value)
	return value
}

// sameModel tells whether an event is about the model being pulled: the router may name it
// with or without its quant, and in any case.
func sameModel(event, model string) bool {
	event, model = strings.ToLower(event), strings.ToLower(model)
	if event == model {
		return true
	}
	repo, _, _ := strings.Cut(model, ":")
	return !strings.Contains(model, ":") && strings.HasPrefix(event, repo+":")
}

// llamaPull starts the router's download and relays its progress, Ollama's style, until it finishes.
func llamaPull(base, key, model string, progress Progress) error {
	if err := llamaModelName(model); err != nil {
		return err
	}
	// Listen first, so no event is missed between starting the download and listening.
	headers := map[string]string{"Accept": "text/event-stream"}
	for name, value := range llamaHeaders(key) {
		headers[name] = value
	}
	events, err := send(http.MethodGet, base+"/models/sse", nil, headers, 0)
	if err != nil {
		return unavailable("llama-server", err)
	}
	defer events.Body.Close()
	if events.StatusCode >= 400 {
		return errors.New("this llama-server can't download models: start it in router mode (without -m)")
	}
	if err := llamaCall(http.MethodPost, base+"/models", key, map[string]any{"model": model}, time.Minute); err != nil {
		return err
	}
	progress(map[string]any{"status": "pulling " + model})
	reader := bufio.NewReader(events.Body)
	for {
		line, err := reader.ReadBytes('\n')
		if text := strings.TrimSpace(string(line)); strings.HasPrefix(text, "data:") {
			var event map[string]any
			if json.Unmarshal([]byte(strings.TrimSpace(strings.TrimPrefix(text, "data:"))), &event) == nil && sameModel(str(event["model"]), model) {
				switch str(event["event"]) {
				case "download_progress":
					var done, total float64
					for _, file := range object(event["data"]) {
						done += number(object(file)["done"])
						total += number(object(file)["total"])
					}
					progress(map[string]any{"status": "downloading", "completed": int64(done), "total": int64(total)})
				case "download_finished":
					// The router lists it once its models are read again.
					_, _, _ = getJSON(base+"/models?reload=1", llamaHeaders(key), 30*time.Second)
					progress(map[string]any{"status": "success"})
					return nil
				case "download_failed":
					text := str(object(event["data"])["error"])
					if text == "" {
						text = str(object(event["data"])["message"])
					}
					if text == "" {
						text = "the download failed"
					}
					progress(map[string]any{"status": "error", "error": text})
					return errors.New(text)
				}
			}
		}
		if err != nil {
			if err == io.EOF {
				return errors.New("llama-server stopped before the download finished")
			}
			return unavailable("llama-server", err)
		}
	}
}

func llamaDelete(base, key, model string) error {
	return llamaCall(http.MethodDelete, base+"/models?model="+url.QueryEscape(model), key, nil, time.Minute)
}

// llamaLog is the log of the llama-server agent-machine started (package servers writes it).
func llamaLog(port, tail int) (string, error) {
	if port != LlamaServerPort() {
		return "", fmt.Errorf("only the llama-server agent-machine starts (on port %d) has a log here", LlamaServerPort())
	}
	text, err := tailFile(filepath.Join(settings.Home(), "llamacpp.log"), tail)
	if errors.Is(err, os.ErrNotExist) {
		return "No log yet: agent-machine writes one when it starts llama.cpp.", nil
	}
	return text, err
}
