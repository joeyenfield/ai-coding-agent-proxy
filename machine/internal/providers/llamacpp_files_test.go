package providers

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
)

// huggingFace plays Hugging Face: a repo listing, and its files with ranges.
type huggingFace struct {
	*httptest.Server
	files  map[string][]byte
	mu     sync.Mutex
	ranges []string
	bad    string
}

func fakeHuggingFace(t *testing.T) *huggingFace {
	t.Helper()
	hf := &huggingFace{files: map[string][]byte{
		"Qwen3-8B-Q4_K_M.gguf":                   []byte(strings.Repeat("q4", 3000)),
		"Q8_0/Qwen3-8B-Q8_0-00001-of-00002.gguf": []byte(strings.Repeat("a", 2500)),
		"Q8_0/Qwen3-8B-Q8_0-00002-of-00002.gguf": []byte(strings.Repeat("b", 1500)),
		"mmproj-BF16.gguf":                       []byte("bf16 projector"),
		"mmproj-F16.gguf":                        []byte("f16 projector"),
	}}
	hf.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/models/someone/Qwen3-8B-GGUF/tree/main" {
			var items []string
			for path, data := range hf.files {
				sum := sha256.Sum256(data)
				digest := hex.EncodeToString(sum[:])
				if path == hf.bad {
					digest = strings.Repeat("0", 64)
				}
				items = append(items, fmt.Sprintf(`{"type":"file","path":%q,"size":%d,"lfs":{"oid":%q,"size":%d}}`, path, len(data), digest, len(data)))
			}
			items = append(items, `{"type":"file","path":"README.md","size":10}`, `{"type":"directory","path":"Q8_0"}`)
			fmt.Fprint(w, "["+strings.Join(items, ",")+"]")
			return
		}
		path, found := strings.CutPrefix(r.URL.Path, "/someone/Qwen3-8B-GGUF/resolve/main/")
		data, ok := hf.files[path]
		if !found || !ok {
			http.NotFound(w, r)
			return
		}
		if from, has := strings.CutPrefix(r.Header.Get("Range"), "bytes="); has {
			start, _ := strconv.Atoi(strings.TrimSuffix(from, "-"))
			hf.mu.Lock()
			hf.ranges = append(hf.ranges, path+":"+strconv.Itoa(start))
			hf.mu.Unlock()
			w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", start, len(data)-1, len(data)))
			w.WriteHeader(http.StatusPartialContent)
			_, _ = w.Write(data[start:])
			return
		}
		_, _ = w.Write(data)
	}))
	t.Cleanup(hf.Close)
	return hf
}

// folderRouter plays an old router serving a GGUF folder: it lists the folder's entries, and
// has no downloads.
func folderRouter(t *testing.T, folder string) *upstream {
	fake := serve(t, map[string]http.HandlerFunc{
		"GET /models": func(w http.ResponseWriter, r *http.Request) {
			var items []any
			if entries, err := os.ReadDir(folder); err == nil {
				for _, entry := range entries {
					items = append(items, map[string]any{"id": entry.Name(), "status": map[string]any{"value": "unloaded"}})
				}
			}
			if items == nil {
				items = []any{}
			}
			reply(map[string]any{"data": items})(w, r)
		},
		"POST /models/unload": reply(map[string]any{"success": true}),
	})
	return fake
}

func ownLlama(t *testing.T, fake *upstream) {
	t.Setenv(LlamaPortEnv, strconv.Itoa(fake.port()))
}

func filesManager(hf *huggingFace) *Manager {
	return &Manager{Machine: on, HuggingFaceURL: hf.URL + "/api/models", HuggingFaceFilesURL: hf.URL}
}

func withFolder(t *testing.T) string {
	folder := filepath.Join(t.TempDir(), "llamacpp-models")
	previous := LlamaFolder
	LlamaFolder = func() string { return folder }
	t.Cleanup(func() { LlamaFolder = previous })
	return folder
}

func TestLlamaFilesPlansTheQuantsShardsAndProjector(t *testing.T) {
	hf := fakeHuggingFace(t)
	folder := withFolder(t)
	answer := filesManager(hf).Handle(Request{Op: "files", Kind: "llamacpp", Port: LlamaServerPort(), Model: "someone/Qwen3-8B-GGUF:q8_0"}, nil)
	plan := answer["result"].(map[string]any)
	files := plan["files"].([]any)
	if answer["ok"] != true || plan["name"] != "Qwen3-8B-Q8_0" || plan["dir"] != filepath.Join(folder, "Qwen3-8B-Q8_0") || len(files) != 3 || plan["serves_folder"] != true {
		t.Fatalf("%v", answer)
	}
	first, projector := files[0].(map[string]any), files[2].(map[string]any)
	if first["url"] != hf.URL+"/someone/Qwen3-8B-GGUF/resolve/main/Q8_0/Qwen3-8B-Q8_0-00001-of-00002.gguf" || first["path"] != filepath.Join("Qwen3-8B-Q8_0", "Qwen3-8B-Q8_0-00001-of-00002.gguf") {
		t.Fatalf("%v", first)
	}
	if projector["repo_path"] != "mmproj-F16.gguf" {
		t.Fatalf("took %v, not the F16 projector", projector["repo_path"])
	}
	// "user/repo" alone is Q4_K_M; a quant the repo hasn't says which it has.
	if plan := filesManager(hf).Handle(Request{Op: "files", Kind: "llamacpp", Port: 1, Model: "someone/Qwen3-8B-GGUF"}, nil)["result"].(map[string]any); plan["model"] != "someone/Qwen3-8B-GGUF:Q4_K_M" || plan["serves_folder"] != false {
		t.Fatalf("%v", plan)
	}
	if answer := filesManager(hf).Handle(Request{Op: "files", Kind: "llamacpp", Port: 1, Model: "someone/Qwen3-8B-GGUF:Q2_K"}, nil); answer["ok"] != false || !strings.Contains(answer["error"].(string), "Q4_K_M, Q8_0") {
		t.Fatalf("%v", answer)
	}
}

func TestADirectPullDownloadsChecksAndServesTheFiles(t *testing.T) {
	hf := fakeHuggingFace(t)
	folder := withFolder(t)
	router := folderRouter(t, folder)
	ownLlama(t, router)
	var updates []map[string]any
	manager := filesManager(hf)
	answer := manager.Handle(Request{Op: "pull", Kind: "llamacpp", Port: router.port(), Model: "someone/Qwen3-8B-GGUF:Q8_0", Method: "direct"}, func(update map[string]any) { updates = append(updates, update) })
	if answer["ok"] != true {
		t.Fatalf("%v", answer)
	}
	for _, name := range []string{"Qwen3-8B-Q8_0-00001-of-00002.gguf", "Qwen3-8B-Q8_0-00002-of-00002.gguf", "mmproj-F16.gguf"} {
		if _, err := os.Stat(filepath.Join(folder, "Qwen3-8B-Q8_0", name)); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
	}
	if last := updates[len(updates)-1]; last["status"] != "success" {
		t.Fatalf("%v", updates)
	}
	// The router deletes only its cache; agent-machine deletes its own folder's models.
	if answer := manager.Handle(Request{Op: "delete", Kind: "llamacpp", Port: router.port(), Model: "Qwen3-8B-Q8_0"}, nil); answer["ok"] != true {
		t.Fatalf("%v", answer)
	}
	if _, err := os.Stat(filepath.Join(folder, "Qwen3-8B-Q8_0")); !os.IsNotExist(err) {
		t.Fatalf("still there: %v", err)
	}
}

func TestAnOldRouterOnItsOwnPortFallsBackToADirectPull(t *testing.T) {
	hf := fakeHuggingFace(t)
	folder := withFolder(t)
	router := folderRouter(t, folder)
	ownLlama(t, router)
	// Half a file from an earlier try: the download carries on from there.
	data := hf.files["Qwen3-8B-Q4_K_M.gguf"]
	_ = os.MkdirAll(filepath.Join(folder, "Qwen3-8B-Q4_K_M"), 0o755)
	_ = os.WriteFile(filepath.Join(folder, "Qwen3-8B-Q4_K_M", "Qwen3-8B-Q4_K_M.gguf.part"), data[:1000], 0o644)
	var updates []string
	answer := filesManager(hf).Handle(Request{Op: "pull", Kind: "llamacpp", Port: router.port(), Model: "someone/Qwen3-8B-GGUF"}, func(update map[string]any) { updates = append(updates, fmt.Sprint(update["status"])) })
	if answer["ok"] != true || !strings.Contains(updates[0], "agent-machine downloads the files itself") {
		t.Fatalf("%v %v", answer, updates)
	}
	got, _ := os.ReadFile(filepath.Join(folder, "Qwen3-8B-Q4_K_M", "Qwen3-8B-Q4_K_M.gguf"))
	if string(got) != string(data) || len(hf.ranges) != 1 || hf.ranges[0] != "Qwen3-8B-Q4_K_M.gguf:1000" {
		t.Fatalf("resumed %v, %d bytes", hf.ranges, len(got))
	}
	// Asked for the router only, it says it's too old.
	if answer := filesManager(hf).Handle(Request{Op: "pull", Kind: "llamacpp", Port: router.port(), Model: "someone/Qwen3-8B-GGUF:Q8_0", Method: "router"}, nil); !strings.Contains(fmt.Sprint(answer["error"]), "too old") {
		t.Fatalf("%v", answer)
	}
}

func TestADownloadThatIsntWhatHuggingFaceListsIsDeleted(t *testing.T) {
	hf := fakeHuggingFace(t)
	hf.bad = "Qwen3-8B-Q4_K_M.gguf"
	folder := withFolder(t)
	router := folderRouter(t, folder)
	ownLlama(t, router)
	answer := filesManager(hf).Handle(Request{Op: "pull", Kind: "llamacpp", Port: router.port(), Model: "someone/Qwen3-8B-GGUF:Q4_K_M", Method: "direct"}, nil)
	if answer["ok"] != false || !strings.Contains(answer["error"].(string), "SHA-256") {
		t.Fatalf("%v", answer)
	}
	entries, _ := os.ReadDir(filepath.Join(folder, "Qwen3-8B-Q4_K_M"))
	if len(entries) != 0 {
		t.Fatalf("left %v", entries)
	}
}

func TestARouterNotServingTheFolderIsRestarted(t *testing.T) {
	hf := fakeHuggingFace(t)
	withFolder(t)
	serving := false
	router := serve(t, map[string]http.HandlerFunc{"GET /models": func(w http.ResponseWriter, r *http.Request) {
		items := []any{}
		if serving {
			items = append(items, map[string]any{"id": "Qwen3-8B-Q4_K_M", "status": map[string]any{"value": "unloaded"}})
		}
		reply(map[string]any{"data": items})(w, r)
	}})
	ownLlama(t, router)
	manager := filesManager(hf)
	manager.RestartLlama = func() error { serving = true; return nil }
	if answer := manager.Handle(Request{Op: "pull", Kind: "llamacpp", Port: router.port(), Model: "someone/Qwen3-8B-GGUF", Method: "direct"}, nil); answer["ok"] != true || !serving {
		t.Fatalf("%v", answer)
	}
	// Another llama-server (a container, another port) doesn't serve agent-machine's folder.
	if answer := manager.Handle(Request{Op: "pull", Kind: "llamacpp", Port: router.port() + 1, Model: "someone/Qwen3-8B-GGUF", Method: "direct"}, nil); answer["ok"] != false {
		t.Fatalf("%v", answer)
	}
}
