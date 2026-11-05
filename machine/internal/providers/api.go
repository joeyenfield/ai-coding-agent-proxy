package providers

import (
	"errors"
	"fmt"
	"time"
)

// errAPIManaged is every op but status on an OpenAI-compatible server: agent-machine only reads
// its model list, since each such server (LM Studio, vLLM, SGLang) manages its models its own way.
var errAPIManaged = errors.New("An OpenAI-compatible server's models are managed in that server itself (LM Studio, vLLM, SGLang, ...)")

// apiStatus is an OpenAI-compatible server's models, from /v1/models.
func apiStatus(base string) map[string]any {
	listed, status, err := getJSON(base+"/v1/models", nil, 5*time.Second)
	if err != nil {
		return map[string]any{"online": false, "error": err.Error(), "models": []any{}}
	}
	if status >= 400 {
		return map[string]any{"online": false, "error": fmt.Sprintf("The OpenAI-compatible server returned %d", status), "models": []any{}}
	}
	data, ok := object(listed)["data"].([]any)
	if !ok {
		return map[string]any{"online": false, "error": "It answered, but not with an OpenAI model list.", "models": []any{}}
	}
	models := []map[string]any{}
	for _, raw := range data {
		item := object(raw)
		id := str(item["id"])
		if id == "" {
			continue
		}
		model := map[string]any{"id": id, "owned_by": str(item["owned_by"])}
		// vLLM says max_model_len, LM Studio loaded_context_length; others context_length.
		for _, key := range []string{"context_length", "max_model_len", "loaded_context_length"} {
			if value, ok := item[key].(float64); ok {
				model["context_length"] = value
				break
			}
		}
		models = append(models, model)
	}
	return map[string]any{"online": true, "version": "", "models": models}
}
