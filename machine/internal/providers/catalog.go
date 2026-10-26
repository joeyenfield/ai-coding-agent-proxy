package providers

// The model catalogues the console searches to pull from: ollama.com's library, GGUF
// repositories on Hugging Face (both for Ollama) and Docker's curated models on Docker
// Hub (for Docker Model Runner). Searched from the machine that will pull the model.

import (
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"ai-coding-agent-proxy/machine/internal/settings"
)

const (
	LibraryURL      = "https://ollama.com/search"
	HuggingFaceURL  = "https://huggingface.co/api/models"
	DockerHubURL    = "https://hub.docker.com/v2"
	hubNamespace    = "ai"
	hubCacheSeconds = 600
	searchAgent     = "Mozilla/5.0 (compatible; agent-machine model search)"
)

// ollama.com/search has no JSON API, so results are parsed from the page. Each model is
// an <a href="/library/NAME" class="group ..."> card holding the description (in a
// <p title>), capability pills (indigo) and size pills (blue).
var (
	libraryCard      = regexp.MustCompile(`(?s)<a href="/library/([^"?#]+)"\s+class="group[^"]*">(.*?)</a>`)
	libraryDesc      = regexp.MustCompile(`<p[^>]*\btitle="([^"]*)"`)
	libraryCap       = regexp.MustCompile(`text-indigo-600[^>]*>([^<]+)<`)
	librarySize      = regexp.MustCompile(`text-blue-600[^>]*>([^<]+)<`)
	libraryDownloads = regexp.MustCompile(`title="([\d,]+)\s+downloads?"`)

	// The quantization at the end of a GGUF filename: Qwen3-8B-UD-Q4_K_XL -> UD-Q4_K_XL.
	hfQuant = regexp.MustCompile(`(?i)(?:^|[^A-Za-z0-9])((?:UD-)?(?:I?Q\d+(?:_[A-Za-z0-9]+)*|BF16|F16|F32|MXFP4(?:_MOE)?))$`)
	hfShard = regexp.MustCompile(`-\d{5}-of-\d{5}$`)
	hubRepo = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$`)
)

func (m *Manager) search(catalog, query, repo string) (map[string]any, error) {
	query = strings.TrimSpace(query)
	switch catalog {
	case "ollama":
		return m.searchLibrary(query, 30)
	case "huggingface":
		return m.searchHuggingFace(query, 25)
	case "huggingface-quants":
		return m.huggingFaceQuants(repo)
	case "docker-hub":
		return m.searchDockerHub(query, 60)
	case "docker-hub-tags":
		return m.dockerHubTags(repo)
	}
	return nil, fmt.Errorf("unknown catalogue %q", catalog)
}

func (m *Manager) external() *http.Client {
	if m.External != nil {
		return m.External
	}
	return settings.Client(20 * time.Second)
}

func orURL(value, fallback string) string {
	if value != "" {
		return value
	}
	return fallback
}

// fetch GETs a catalogue page; status is the answer's.
func (m *Manager) fetch(address string, params url.Values) ([]byte, int, error) {
	if len(params) > 0 {
		address += "?" + params.Encode()
	}
	request, err := http.NewRequest(http.MethodGet, address, nil)
	if err != nil {
		return nil, 0, err
	}
	request.Header.Set("User-Agent", searchAgent)
	response, err := m.external().Do(request)
	if err != nil {
		return nil, 0, err
	}
	defer response.Body.Close()
	data, err := io.ReadAll(io.LimitReader(response.Body, 16<<20))
	return data, response.StatusCode, err
}

func (m *Manager) searchLibrary(query string, limit int) (map[string]any, error) {
	page, status, err := m.fetch(orURL(m.LibraryURL, LibraryURL), url.Values{"q": {query}})
	if err == nil && status >= 400 {
		err = fmt.Errorf("status %d", status)
	}
	if err != nil {
		return nil, fmt.Errorf("Couldn't reach ollama.com: %v", err)
	}
	models := ParseLibrary(string(page))
	if len(models) > limit {
		models = models[:limit]
	}
	var note any
	if len(models) == 0 {
		note = "No models found. Try a different search, or paste an exact tag into a machine's Pull box."
	}
	return map[string]any{"query": query, "models": models, "note": note}, nil
}

// ParseLibrary extracts the model cards from an ollama.com/search page.
func ParseLibrary(page string) []map[string]any {
	models := []map[string]any{}
	seen := map[string]bool{}
	for _, match := range libraryCard.FindAllStringSubmatch(page, -1) {
		name, block := strings.TrimSpace(match[1]), match[2]
		if name == "" || strings.Contains(name, "/") || seen[name] {
			continue
		}
		seen[name] = true
		description := ""
		if found := libraryDesc.FindStringSubmatch(block); found != nil {
			description = strings.TrimSpace(html.UnescapeString(found[1]))
		}
		capabilities := []string{}
		for _, found := range libraryCap.FindAllStringSubmatch(block, -1) {
			capabilities = append(capabilities, strings.TrimSpace(html.UnescapeString(found[1])))
		}
		sizes := []string{}
		known := map[string]bool{}
		for _, found := range librarySize.FindAllStringSubmatch(block, -1) {
			size := strings.ToLower(strings.TrimSpace(found[1]))
			if size != "" && !known[size] {
				known[size] = true
				sizes = append(sizes, size)
			}
		}
		var pulls any
		if found := libraryDownloads.FindStringSubmatch(block); found != nil {
			if count, err := strconv.Atoi(strings.ReplaceAll(found[1], ",", "")); err == nil {
				pulls = count
			}
		}
		models = append(models, map[string]any{"name": name, "description": description, "capabilities": capabilities, "sizes": sizes, "pulls": pulls})
	}
	return models
}

func (m *Manager) searchHuggingFace(query string, limit int) (map[string]any, error) {
	if query == "" {
		return map[string]any{"query": "", "repos": []any{}, "note": "Type to search Hugging Face for GGUF models."}, nil
	}
	params := url.Values{"search": {query}, "filter": {"gguf"}, "sort": {"downloads"}, "direction": {"-1"}, "limit": {strconv.Itoa(limit)}}
	data, status, err := m.fetch(orURL(m.HuggingFaceURL, HuggingFaceURL), params)
	var items []any
	if err == nil && status >= 400 {
		err = fmt.Errorf("status %d", status)
	}
	if err == nil {
		err = json.Unmarshal(data, &items)
	}
	if err != nil {
		return nil, fmt.Errorf("Couldn't reach Hugging Face: %v", err)
	}
	repos := []map[string]any{}
	for _, raw := range items {
		item := object(raw)
		if str(item["id"]) != "" {
			repos = append(repos, map[string]any{"repo": item["id"], "downloads": item["downloads"], "likes": item["likes"]})
		}
	}
	var note any
	if len(repos) == 0 {
		note = "No GGUF repositories found. Try a different search."
	}
	return map[string]any{"query": query, "repos": repos, "note": note}, nil
}

func (m *Manager) huggingFaceQuants(repo string) (map[string]any, error) {
	repo = strings.Trim(strings.TrimSpace(repo), "/")
	if repo == "" || strings.Count(repo, "/") != 1 {
		return nil, errors.New("Use the form user/repo")
	}
	data, status, err := m.fetch(orURL(m.HuggingFaceURL, HuggingFaceURL)+"/"+repo+"/tree/main", url.Values{"recursive": {"1"}})
	switch {
	case err != nil:
		return nil, fmt.Errorf("Couldn't reach Hugging Face: %v", err)
	case status == http.StatusUnauthorized || status == http.StatusForbidden:
		return nil, errors.New("This repo is gated or private. Accept its licence on huggingface.co first.")
	case status == http.StatusNotFound:
		return nil, errors.New("Repo not found on Hugging Face.")
	case status >= 400:
		return nil, fmt.Errorf("Hugging Face returned %d", status)
	}
	var files any
	if json.Unmarshal(data, &files) != nil {
		return nil, errors.New("Unexpected response from Hugging Face")
	}
	quants := ParseQuants(files)
	var note any
	if len(quants) == 0 {
		note = "No GGUF files here. Ollama can only pull GGUF models; look for a repo ending in -GGUF."
	}
	return map[string]any{"repo": repo, "quants": quants, "note": note}, nil
}

// QuantFromFilename is 'Qwen3-8B-UD-Q4_K_XL' -> 'UD-Q4_K_XL', or "" without a quantization.
func QuantFromFilename(stem string) string {
	match := hfQuant.FindStringSubmatch(hfShard.ReplaceAllString(stem, ""))
	if match == nil {
		return ""
	}
	return strings.ToUpper(match[1])
}

// ParseQuants sums up the GGUF quantizations in a Hugging Face repo listing, smallest first.
func ParseQuants(files any) []map[string]any {
	quants := map[string]map[string]any{}
	order := []string{}
	for _, raw := range list(files) {
		item := object(raw)
		if str(item["type"]) != "file" {
			continue
		}
		path := str(item["path"])
		name := path[strings.LastIndex(path, "/")+1:]
		lowered := strings.ToLower(name)
		if !strings.HasSuffix(lowered, ".gguf") || strings.Contains(lowered, "mmproj") {
			continue
		}
		stem := name[:len(name)-5]
		quant := QuantFromFilename(stem)
		if quant == "" {
			continue
		}
		entry, ok := quants[quant]
		if !ok {
			entry = map[string]any{"quant": quant, "size": int64(0), "split": false}
			quants[quant] = entry
			order = append(order, quant)
		}
		size := number(object(item["lfs"])["size"])
		if size == 0 {
			size = number(item["size"])
		}
		entry["size"] = entry["size"].(int64) + int64(size)
		entry["split"] = entry["split"].(bool) || hfShard.MatchString(stem)
	}
	result := []map[string]any{}
	for _, quant := range order {
		result = append(result, quants[quant])
	}
	sort.SliceStable(result, func(i, j int) bool { return result[i]["size"].(int64) < result[j]["size"].(int64) })
	return result
}

type hubCache struct {
	mu      sync.Mutex
	entries map[string]hubEntry
}

type hubEntry struct {
	at     time.Time
	models []map[string]any
}

// searchDockerHub is Docker's curated models (the ai/ namespace), most pulled first; with
// a query, those named like it. Docker Hub only gives anonymous clients the first 100
// results, so the name filter runs on its side. Cached for ten minutes.
func (m *Manager) searchDockerHub(query string, limit int) (map[string]any, error) {
	key := strings.ToLower(query)
	m.hub.mu.Lock()
	cached, ok := m.hub.entries[key]
	m.hub.mu.Unlock()
	models := cached.models
	if !ok || time.Since(cached.at) > hubCacheSeconds*time.Second {
		params := url.Values{"page_size": {"100"}, "ordering": {"pull_count"}}
		if key != "" {
			params.Set("name", key)
		}
		data, status, err := m.fetch(orURL(m.DockerHubURL, DockerHubURL)+"/repositories/"+hubNamespace+"/", params)
		var page any
		if err == nil && status >= 400 {
			err = fmt.Errorf("status %d", status)
		}
		if err == nil {
			err = json.Unmarshal(data, &page)
		}
		if err != nil {
			return nil, fmt.Errorf("Couldn't reach Docker Hub: %v", err)
		}
		models = ParseHubRepositories(page)
		m.hub.mu.Lock()
		if m.hub.entries == nil || len(m.hub.entries) > 64 {
			m.hub.entries = map[string]hubEntry{}
		}
		m.hub.entries[key] = hubEntry{at: time.Now(), models: models}
		m.hub.mu.Unlock()
	}
	if len(models) > limit {
		models = models[:limit]
	}
	var note any
	if len(models) == 0 {
		note = "No Docker Hub models have that in their name. Pull any other model by name, such as hf.co/<user>/<repo>."
	}
	return map[string]any{"query": query, "models": models, "note": note}, nil
}

func ParseHubRepositories(page any) []map[string]any {
	models := []map[string]any{}
	for _, raw := range list(object(page)["results"]) {
		item := object(raw)
		if str(item["name"]) == "" {
			continue
		}
		namespace := str(item["namespace"])
		if namespace == "" {
			namespace = hubNamespace
		}
		models = append(models, map[string]any{
			"name": namespace + "/" + str(item["name"]), "description": str(item["description"]),
			"pulls": item["pull_count"], "stars": item["star_count"], "updated": item["last_updated"],
		})
	}
	return models
}

func (m *Manager) dockerHubTags(repo string) (map[string]any, error) {
	repo = strings.ToLower(strings.Trim(strings.TrimSpace(repo), "/"))
	if !hubRepo.MatchString(repo) {
		return nil, errors.New("Use the form namespace/model, such as ai/qwen3")
	}
	data, status, err := m.fetch(orURL(m.DockerHubURL, DockerHubURL)+"/repositories/"+repo+"/tags", url.Values{"page_size": {"100"}})
	switch {
	case err != nil:
		return nil, fmt.Errorf("Couldn't reach Docker Hub: %v", err)
	case status == http.StatusNotFound:
		return nil, fmt.Errorf("%s isn't on Docker Hub.", repo)
	case status >= 400:
		return nil, fmt.Errorf("Docker Hub returned %d", status)
	}
	var page any
	if json.Unmarshal(data, &page) != nil {
		return nil, errors.New("Unexpected response from Docker Hub")
	}
	return map[string]any{"repo": repo, "tags": ParseHubTags(page)}, nil
}

// TagEngine is the engine a Docker Hub tag needs beyond llama.cpp: vllm for safetensors
// weights, mlx for Apple MLX ones; nil for llama.cpp.
func TagEngine(tag string) any {
	lowered := strings.ToLower(tag)
	if strings.Contains(lowered, "safetensors") || strings.HasSuffix(lowered, "vllm") {
		return "vllm"
	}
	if strings.Contains(lowered, "mlx") {
		return "mlx"
	}
	return nil
}

// ParseHubTags is a model's tags, latest first, then smallest.
func ParseHubTags(page any) []map[string]any {
	tags := []map[string]any{}
	for _, raw := range list(object(page)["results"]) {
		item := object(raw)
		name := str(item["name"])
		if name == "" {
			continue
		}
		tags = append(tags, map[string]any{"tag": name, "size": item["full_size"], "engine": TagEngine(name), "updated": item["last_updated"]})
	}
	sort.SliceStable(tags, func(i, j int) bool {
		latestI, latestJ := tags[i]["tag"] == "latest", tags[j]["tag"] == "latest"
		if latestI != latestJ {
			return latestI
		}
		return number(tags[i]["size"]) < number(tags[j]["size"])
	})
	return tags
}
