package providers

// Docker Model Runner, Docker's own model server. Routes used, from docker/model-runner
// (pkg/inference):
//
//	GET    /engines/status    inference engines and their state; identifies Model Runner
//	GET    /models            pulled models: id, tags and config (format, quantization, size, ...)
//	GET    /engines/ps        models loaded right now
//	GET    /engines/df        disk used by models
//	POST   /models/create     pull; JSON progress lines when sent Accept: application/json
//	DELETE /models/{ref}      delete a model
//	POST   /engines/unload    free a loaded model's memory
//	POST   /engines/v1/chat/completions with X-Preload-Only: true    load a model without generating

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"
)

var registryPrefixes = []string{"index.docker.io/", "docker.io/", "registry-1.docker.io/"}

// normalizeRef makes 'docker.io/ai/qwen3:latest' and 'ai/qwen3' the same.
func normalizeRef(ref string) string {
	value := strings.ToLower(strings.TrimSpace(ref))
	for _, prefix := range registryPrefixes {
		if strings.HasPrefix(value, prefix) {
			value = value[len(prefix):]
			break
		}
	}
	return strings.TrimSuffix(value, ":latest")
}

// displayName is the name agents send: the tag without the registry or ':latest'.
func displayName(tag string) string {
	for _, prefix := range registryPrefixes {
		if strings.HasPrefix(tag, prefix) {
			tag = tag[len(prefix):]
			break
		}
	}
	return strings.TrimSuffix(tag, ":latest")
}

// modelPath is a model reference as a URL path, keeping its slashes: ai/qwen3:8B-Q4_K_M.
func modelPath(name string) string {
	parts := strings.Split(strings.TrimSpace(name), "/")
	for i, part := range parts {
		parts[i] = strings.NewReplacer("%3A", ":", "%40", "@").Replace(url.PathEscape(part))
	}
	return strings.Join(parts, "/")
}

func tagsOf(item map[string]any) []string {
	var tags []string
	for _, tag := range list(item["tags"]) {
		if text := str(tag); text != "" {
			tags = append(tags, text)
		}
	}
	return tags
}

// dockerModels is the pulled models, newest first, with whether each is loaded now.
func dockerModels(listed, running any) []map[string]any {
	loaded := map[string]map[string]any{}
	for _, item := range list(running) {
		entry := object(item)
		if name := str(entry["model_name"]); name != "" {
			loaded[normalizeRef(name)] = entry
		}
	}
	models := []map[string]any{}
	for _, raw := range list(listed) {
		item := object(raw)
		id := str(item["id"])
		if id == "" {
			continue
		}
		tags := tagsOf(item)
		config := object(item["config"])
		keys := []string{normalizeRef(id)}
		for _, tag := range tags {
			keys = append(keys, normalizeRef(tag))
		}
		var state map[string]any
		for _, key := range keys {
			if found, ok := loaded[key]; ok {
				state = found
				break
			}
		}
		name := id
		if len(tags) > 0 {
			name = displayName(tags[0])
		}
		shown := []string{}
		for _, tag := range tags {
			shown = append(shown, displayName(tag))
		}
		model := map[string]any{
			"id": id, "name": name, "tags": shown, "created": item["created"],
			"format": config["format"], "quantization": config["quantization"], "parameters": config["parameters"],
			"architecture": config["architecture"], "size": config["size"], "context_size": config["context_size"],
			"loaded": false, "loading": false, "in_use": false, "engine": nil,
		}
		if state != nil {
			loading, _ := state["loading"].(bool)
			inUse, _ := state["in_use"].(bool)
			model["loaded"], model["loading"], model["in_use"], model["engine"] = !loading, loading, inUse, state["backend_name"]
		}
		models = append(models, model)
	}
	sort.SliceStable(models, func(i, j int) bool { return number(models[i]["created"]) > number(models[j]["created"]) })
	return models
}

// runningRefs is the names a model is loaded under now: a request may have loaded it by any of its tags.
func runningRefs(listed, running any, name string) []string {
	keys := map[string]bool{normalizeRef(name): true}
	for _, raw := range list(listed) {
		item := object(raw)
		names := []string{normalizeRef(str(item["id"]))}
		for _, tag := range tagsOf(item) {
			names = append(names, normalizeRef(tag))
		}
		match := false
		for _, candidate := range names {
			match = match || keys[candidate]
		}
		if match {
			for _, candidate := range names {
				keys[candidate] = true
			}
		}
	}
	refs := []string{}
	for _, raw := range list(running) {
		if model := str(object(raw)["model_name"]); model != "" && keys[normalizeRef(model)] {
			refs = append(refs, model)
		}
	}
	return refs
}

func dockerStatus(base string) map[string]any {
	empty := func(online bool, err string, engines any) map[string]any {
		if engines == nil {
			engines = map[string]any{}
		}
		return map[string]any{"online": online, "error": err, "engines": engines, "models": []any{}, "disk": nil}
	}
	engines, status, err := getJSON(base+"/engines/status", nil, 4*time.Second)
	if err != nil {
		return empty(false, err.Error(), nil)
	}
	states, ok := engines.(map[string]any)
	for _, value := range states {
		if _, text := value.(string); !text {
			ok = false
		}
	}
	if status >= 400 || !ok {
		return empty(false, "It answered, but not as Docker Model Runner.", nil)
	}
	var (
		wait                     sync.WaitGroup
		listed, running, disk    any
		listStatus, runStatus    int
		diskStatus               int
		listErr, runErr, diskErr error
	)
	wait.Add(3)
	go func() { defer wait.Done(); listed, listStatus, listErr = getJSON(base+"/models", nil, 8*time.Second) }()
	go func() {
		defer wait.Done()
		running, runStatus, runErr = getJSON(base+"/engines/ps", nil, 4*time.Second)
	}()
	go func() { defer wait.Done(); disk, diskStatus, diskErr = getJSON(base+"/engines/df", nil, 4*time.Second) }()
	wait.Wait()
	if listErr != nil || listStatus >= 400 {
		detail := fmt.Sprintf("status %d", listStatus)
		if listErr != nil {
			detail = listErr.Error()
		}
		return empty(true, "Model Runner couldn't list its models: "+detail, states)
	}
	if runErr != nil || runStatus >= 400 {
		running = nil
	}
	var usage any
	if found, isObject := disk.(map[string]any); diskErr == nil && diskStatus < 400 && isObject {
		usage = found
	}
	return map[string]any{"online": true, "engines": states, "models": dockerModels(listed, running), "disk": usage}
}

// pullProgress turns one of Model Runner's pull messages into Ollama-style progress.
// layers collects the bytes done per layer, so completed covers the whole model.
func pullProgress(message map[string]any, layers map[string]float64) map[string]any {
	switch str(message["type"]) {
	case "error":
		text := str(message["message"])
		if text == "" {
			text = "Pull failed"
		}
		return map[string]any{"status": "error", "error": text}
	case "success":
		return map[string]any{"status": "success"}
	case "warning":
		text := str(message["message"])
		if text == "" {
			text = "warning"
		}
		return map[string]any{"status": text}
	case "progress":
	default:
		return nil
	}
	layer := object(message["layer"])
	layers[str(layer["id"])] = number(layer["current"])
	total := number(message["total"])
	completed := 0.0
	for _, done := range layers {
		completed += done
	}
	update := map[string]any{"status": "downloading", "total": nil, "completed": int64(completed)}
	if total > 0 {
		update["total"], update["completed"] = int64(total), int64(min(completed, total))
	}
	return update
}

func dockerPull(base, model string, progress Progress) error {
	response, err := send(http.MethodPost, base+"/models/create", map[string]any{"from": model}, map[string]string{"Accept": "application/json"}, 0)
	if err != nil {
		return unavailable("Docker Model Runner", err)
	}
	defer response.Body.Close()
	if response.StatusCode >= 400 {
		data, _ := io.ReadAll(io.LimitReader(response.Body, 64<<10))
		return errors.New(errorText(data, response.StatusCode, "Model Runner"))
	}
	layers := map[string]float64{}
	var failed error
	err = streamLines(response.Body, func(line []byte) {
		var message map[string]any
		if json.Unmarshal(line, &message) != nil {
			return
		}
		if update := pullProgress(message, layers); update != nil {
			if update["status"] == "error" {
				failed = errors.New(str(update["error"]))
			}
			progress(update)
		}
	})
	if failed != nil {
		return failed
	}
	return err
}

func dockerCall(method, url string, body any, headers map[string]string, timeout time.Duration) (any, error) {
	response, err := send(method, url, body, headers, timeout)
	if err != nil {
		return nil, unavailable("Docker Model Runner", err)
	}
	defer response.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(response.Body, 16<<20))
	if response.StatusCode >= 400 {
		return nil, errors.New(errorText(data, response.StatusCode, "Docker Model Runner"))
	}
	var value any
	_ = json.Unmarshal(data, &value)
	return value, nil
}

// dockerUnload frees a model under every name it's loaded by.
func dockerUnload(base, model string) (any, error) {
	listed, err := dockerCall(http.MethodGet, base+"/models", nil, nil, 8*time.Second)
	if err != nil {
		return nil, err
	}
	running, err := dockerCall(http.MethodGet, base+"/engines/ps", nil, nil, 4*time.Second)
	if err != nil {
		return nil, err
	}
	refs := runningRefs(listed, running, model)
	if len(refs) == 0 {
		refs = []string{model}
	}
	answer, err := dockerCall(http.MethodPost, base+"/engines/unload", map[string]any{"models": refs}, nil, time.Minute)
	if err != nil {
		return nil, err
	}
	return object(answer)["unloaded_runners"], nil
}
