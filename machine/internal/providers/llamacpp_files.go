package providers

// Downloading a llama.cpp model's GGUF files here, instead of through the router: for routers
// too old to download (llama.cpp before mid-2026, which stale Termux mirrors still hand out), or
// by choice. The files go into the GGUF folder agent-machine's llama-server serves
// (LLAMA_ARG_MODELS_DIR), one subfolder per model, which every router reads and picks up again
// on /models?reload=1. "files" gives the same plan for downloading them by hand (curl).

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"hash"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"ai-coding-agent-proxy/machine/internal/settings"
)

// HuggingFaceFilesURL is where Hugging Face serves a repo's files (<repo>/resolve/main/<path>).
const HuggingFaceFilesURL = "https://huggingface.co"

// DefaultQuant is what "user/repo" alone downloads, as llama-server's -hf does.
const DefaultQuant = "Q4_K_M"

// LlamaFolder is the GGUF folder agent-machine's llama-server serves. Package servers, which
// owns llama.cpp's settings, replaces it with the configured one.
var LlamaFolder = func() string { return filepath.Join(settings.Home(), "llamacpp-models") }

var safeName = regexp.MustCompile(`[^A-Za-z0-9._-]+`)

// llamaFile is one file to download: its path in the repo, and in the GGUF folder.
type llamaFile struct {
	Repo   string `json:"repo_path"`
	Path   string `json:"path"`
	URL    string `json:"url"`
	Size   int64  `json:"size"`
	SHA256 string `json:"sha256"`
}

// llamaPlan is what downloading a model means: which files, into which folder.
type llamaPlan struct {
	Model  string      `json:"model"`
	Name   string      `json:"name"`
	Folder string      `json:"folder"`
	Dir    string      `json:"dir"`
	Files  []llamaFile `json:"files"`
}

func (p llamaPlan) public() map[string]any {
	var files []any
	for _, file := range p.Files {
		files = append(files, map[string]any{"repo_path": file.Repo, "path": file.Path, "url": file.URL, "size": file.Size, "sha256": file.SHA256})
	}
	return map[string]any{"model": p.Model, "name": p.Name, "folder": p.Folder, "dir": p.Dir, "files": files}
}

// hfToken is a Hugging Face token for gated repos, as llama.cpp itself reads it.
func hfToken() string { return strings.TrimSpace(os.Getenv("HF_TOKEN")) }

// llamaPlanFor finds a model's files on Hugging Face: the quant's GGUF (every shard of a split
// one) and, as llama-server's -hf does, the repo's vision projector.
func (m *Manager) llamaPlanFor(model string) (llamaPlan, error) {
	if err := llamaModelName(model); err != nil {
		return llamaPlan{}, err
	}
	repo, quant, _ := strings.Cut(model, ":")
	headers := map[string]string{}
	if token := hfToken(); token != "" {
		headers["Authorization"] = "Bearer " + token
	}
	data, status, err := m.fetchWith(orURL(m.HuggingFaceURL, HuggingFaceURL)+"/"+repo+"/tree/main", url.Values{"recursive": {"1"}}, headers)
	switch {
	case err != nil:
		return llamaPlan{}, fmt.Errorf("couldn't reach Hugging Face: %v", err)
	case status == http.StatusUnauthorized || status == http.StatusForbidden:
		return llamaPlan{}, errors.New("this repo is gated or private: accept its licence on huggingface.co, and give agent-machine a token (HF_TOKEN)")
	case status == http.StatusNotFound:
		return llamaPlan{}, fmt.Errorf("%s isn't on Hugging Face", repo)
	case status >= 400:
		return llamaPlan{}, fmt.Errorf("Hugging Face returned %d", status)
	}
	var listed any
	if json.Unmarshal(data, &listed) != nil {
		return llamaPlan{}, errors.New("unexpected answer from Hugging Face")
	}
	byQuant := map[string][]map[string]any{}
	var projectors []map[string]any
	for _, raw := range list(listed) {
		item := object(raw)
		path := str(item["path"])
		name := strings.ToLower(path[strings.LastIndex(path, "/")+1:])
		if str(item["type"]) != "file" || !strings.HasSuffix(name, ".gguf") {
			continue
		}
		if strings.Contains(name, "mmproj") {
			projectors = append(projectors, item)
			continue
		}
		stem := path[strings.LastIndex(path, "/")+1 : len(path)-5]
		if found := QuantFromFilename(stem); found != "" {
			byQuant[found] = append(byQuant[found], item)
		}
	}
	available := make([]string, 0, len(byQuant))
	for found := range byQuant {
		available = append(available, found)
	}
	sort.Strings(available)
	quant = strings.ToUpper(strings.TrimSpace(quant))
	if quant == "" {
		quant = DefaultQuant
		if _, ok := byQuant[quant]; !ok && len(available) == 1 {
			quant = available[0]
		}
	}
	chosen := byQuant[quant]
	if len(chosen) == 0 {
		if len(available) == 0 {
			return llamaPlan{}, fmt.Errorf("%s has no GGUF files: look for a repo ending in -GGUF", repo)
		}
		return llamaPlan{}, fmt.Errorf("%s has no %s: it has %s", repo, quant, strings.Join(available, ", "))
	}
	sort.Slice(chosen, func(i, j int) bool { return str(chosen[i]["path"]) < str(chosen[j]["path"]) })
	if projector := pickProjector(projectors); projector != nil {
		chosen = append(chosen, projector)
	}
	short := repo[strings.Index(repo, "/")+1:]
	if strings.HasSuffix(strings.ToLower(short), "-gguf") {
		short = short[:len(short)-5]
	}
	name := strings.Trim(safeName.ReplaceAllString(short+"-"+quant, "-"), "-.")
	folder := LlamaFolder()
	plan := llamaPlan{Model: repo + ":" + quant, Name: name, Folder: folder, Dir: filepath.Join(folder, name)}
	base := orURL(m.HuggingFaceFilesURL, HuggingFaceFilesURL)
	for _, item := range chosen {
		path := str(item["path"])
		lfs := object(item["lfs"])
		size := int64(number(lfs["size"]))
		if size == 0 {
			size = int64(number(item["size"]))
		}
		segments := strings.Split(path, "/")
		for i, segment := range segments {
			segments[i] = url.PathEscape(segment)
		}
		file := segments[len(segments)-1]
		local, _ := url.PathUnescape(file)
		plan.Files = append(plan.Files, llamaFile{
			Repo: path, Path: filepath.Join(name, local), Size: size, SHA256: strings.ToLower(str(lfs["oid"])),
			URL: base + "/" + repo + "/resolve/main/" + strings.Join(segments, "/"),
		})
	}
	return plan, nil
}

// pickProjector is the projector llama.cpp would take: F16 first, then BF16, then any.
func pickProjector(projectors []map[string]any) map[string]any {
	for _, want := range []string{"f16", "bf16", ""} {
		for _, item := range projectors {
			name := strings.ToLower(str(item["path"]))
			if want == "" || (strings.Contains(name, want) && !(want == "f16" && strings.Contains(name, "bf16"))) {
				return item
			}
		}
	}
	return nil
}

// llamaOwn tells whether a port is the llama-server agent-machine starts, the one serving its GGUF folder.
func llamaOwn(port int) bool { return port == LlamaServerPort() }

// llamaFetch downloads a model's files into its subfolder of the GGUF folder, each checked by
// its SHA-256, then has the router read the folder again.
func (m *Manager) llamaFetch(base, key, model string, port int, progress Progress) error {
	if !llamaOwn(port) {
		return fmt.Errorf("only the llama-server agent-machine starts (port %d) serves its GGUF folder: download with the router itself, or into this one's own folder by hand", LlamaServerPort())
	}
	plan, err := m.llamaPlanFor(model)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(plan.Dir, 0o755); err != nil {
		return err
	}
	var total, done int64
	for _, file := range plan.Files {
		total += file.Size
	}
	progress(map[string]any{"status": "downloading " + plan.Model + " into " + plan.Dir, "completed": int64(0), "total": total})
	for _, file := range plan.Files {
		target := filepath.Join(plan.Folder, file.Path)
		if info, err := os.Stat(target); err == nil && (file.Size == 0 || info.Size() == file.Size) {
			done += info.Size()
			continue
		}
		written, err := m.download(file, target+".part", func(n int64) {
			progress(map[string]any{"status": "downloading", "completed": done + n, "total": total})
		})
		if err != nil {
			return err
		}
		done += written
	}
	// Only now take .part off, so the router never lists a model whose files aren't all there.
	for _, file := range plan.Files {
		target := filepath.Join(plan.Folder, file.Path)
		if _, err := os.Stat(target + ".part"); err == nil {
			if err := os.Rename(target+".part", target); err != nil {
				return err
			}
		}
	}
	return m.llamaServe(base, key, plan.Name, progress)
}

// llamaServe has the router read its GGUF folder again and checks it lists name; a router
// started before it served that folder has to restart for it.
func (m *Manager) llamaServe(base, key, name string, progress Progress) error {
	listed := func() bool {
		data, status, err := getJSON(base+"/models?reload=1", llamaHeaders(key), 30*time.Second)
		if err != nil || status >= 400 {
			return false
		}
		for _, raw := range list(object(data)["data"]) {
			if strings.EqualFold(str(object(raw)["id"]), name) {
				return true
			}
		}
		return false
	}
	if listed() {
		progress(map[string]any{"status": "success"})
		return nil
	}
	if m.RestartLlama == nil {
		return fmt.Errorf("downloaded into %s, but llama-server doesn't list it: restart llama.cpp from the machine's llama.cpp server tab", LlamaFolder())
	}
	progress(map[string]any{"status": "restarting llama.cpp to serve " + LlamaFolder()})
	if err := m.RestartLlama(); err != nil {
		return fmt.Errorf("downloaded into %s, but couldn't restart llama.cpp to serve it: %v", LlamaFolder(), err)
	}
	if !listed() {
		return fmt.Errorf("downloaded into %s, but llama-server doesn't list %s: check its GGUF folder setting", LlamaFolder(), name)
	}
	progress(map[string]any{"status": "success"})
	return nil
}

// download writes one file to part, resuming what an earlier try left, and checks its SHA-256.
// It reports the bytes it has so far and returns the file's size.
func (m *Manager) download(file llamaFile, part string, report func(int64)) (int64, error) {
	digest := sha256.New()
	var have int64
	if existing, err := os.Open(part); err == nil {
		have, _ = io.Copy(digest, existing)
		existing.Close()
	}
	if file.Size > 0 && have > file.Size {
		have, digest = 0, sha256.New()
	}
	request, err := http.NewRequest(http.MethodGet, file.URL, nil)
	if err != nil {
		return 0, err
	}
	request.Header.Set("User-Agent", searchAgent)
	if token := hfToken(); token != "" {
		request.Header.Set("Authorization", "Bearer "+token)
	}
	if have > 0 {
		request.Header.Set("Range", fmt.Sprintf("bytes=%d-", have))
	}
	client := m.Downloads
	if client == nil {
		client = settings.Client(0)
	}
	response, err := client.Do(request)
	if err != nil {
		return 0, fmt.Errorf("couldn't download %s: %v", file.Repo, err)
	}
	defer response.Body.Close()
	flags := os.O_CREATE | os.O_WRONLY | os.O_APPEND
	switch {
	case response.StatusCode == http.StatusPartialContent && have > 0:
	case response.StatusCode == http.StatusOK:
		// Started again from the beginning (no range, or the server ignored it).
		have, digest, flags = 0, sha256.New(), os.O_CREATE|os.O_WRONLY|os.O_TRUNC
	case response.StatusCode == http.StatusRequestedRangeNotSatisfiable && file.Size > 0 && have == file.Size:
		return have, checkDigest(file, digest)
	case response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden:
		return 0, errors.New("Hugging Face refused the download: the repo is gated or private (accept its licence, and give agent-machine a token: HF_TOKEN)")
	default:
		return 0, fmt.Errorf("Hugging Face answered %d for %s", response.StatusCode, file.Repo)
	}
	out, err := os.OpenFile(part, flags, 0o644)
	if err != nil {
		return 0, err
	}
	written, err := copyReporting(io.MultiWriter(out, digest), response.Body, have, report)
	if closeErr := out.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return 0, fmt.Errorf("the download of %s stopped: %v (pull it again to carry on)", file.Repo, err)
	}
	if file.Size > 0 && written != file.Size {
		return 0, fmt.Errorf("the download of %s stopped at %d of %d bytes (pull it again to carry on)", file.Repo, written, file.Size)
	}
	if err := checkDigest(file, digest); err != nil {
		_ = os.Remove(part)
		return 0, err
	}
	return written, nil
}

func checkDigest(file llamaFile, digest hash.Hash) error {
	if file.SHA256 == "" {
		return nil
	}
	if got := hex.EncodeToString(digest.Sum(nil)); got != file.SHA256 {
		return fmt.Errorf("%s isn't what Hugging Face lists (SHA-256 %s, not %s): deleted it, pull it again", file.Repo, got, file.SHA256)
	}
	return nil
}

// copyReporting copies, reporting the running total (from start) about once a second.
func copyReporting(dst io.Writer, src io.Reader, start int64, report func(int64)) (int64, error) {
	buffer := make([]byte, 1<<20)
	total, last := start, time.Time{}
	for {
		n, err := src.Read(buffer)
		if n > 0 {
			if _, werr := dst.Write(buffer[:n]); werr != nil {
				return total, werr
			}
			total += int64(n)
			if time.Since(last) >= time.Second {
				report(total)
				last = time.Now()
			}
		}
		if err == io.EOF {
			report(total)
			return total, nil
		}
		if err != nil {
			return total, err
		}
	}
}

// llamaRemoveLocal deletes a model agent-machine downloaded into its GGUF folder (the router
// deletes only what's in its cache). It says whether name was one.
func (m *Manager) llamaRemoveLocal(base, key, name string) (bool, error) {
	if name == "" || name != filepath.Base(name) || strings.HasPrefix(name, ".") {
		return false, nil
	}
	folder := LlamaFolder()
	for _, path := range []string{filepath.Join(folder, name), filepath.Join(folder, name+".gguf")} {
		if _, err := os.Stat(path); err != nil {
			continue
		}
		// A loaded model keeps its files open; unloading first lets Windows delete them too.
		_ = llamaCall(http.MethodPost, base+"/models/unload", key, map[string]any{"model": name}, time.Minute)
		if err := os.RemoveAll(path); err != nil {
			return true, err
		}
		_, _, _ = getJSON(base+"/models?reload=1", llamaHeaders(key), 30*time.Second)
		return true, nil
	}
	return false, nil
}

// fetchWith is fetch with extra headers.
func (m *Manager) fetchWith(address string, params url.Values, headers map[string]string) ([]byte, int, error) {
	if len(params) > 0 {
		address += "?" + params.Encode()
	}
	request, err := http.NewRequest(http.MethodGet, address, nil)
	if err != nil {
		return nil, 0, err
	}
	request.Header.Set("User-Agent", searchAgent)
	for name, value := range headers {
		request.Header.Set(name, value)
	}
	response, err := m.external().Do(request)
	if err != nil {
		return nil, 0, err
	}
	defer response.Body.Close()
	data, err := io.ReadAll(io.LimitReader(response.Body, 16<<20))
	return data, response.StatusCode, err
}
