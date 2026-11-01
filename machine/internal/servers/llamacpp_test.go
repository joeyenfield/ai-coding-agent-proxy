package servers

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"ai-coding-agent-proxy/machine/internal/settings"
)

func TestLlamaSettingsAreCheckedAndSaved(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	if config := LoadLlamaConfig(); config["ctx_size"] != int64(16384) || config["fit"] != true {
		t.Fatalf("defaults: %v", config)
	}
	for _, refused := range []map[string]any{
		{"ctx_size": "big"},
		{"flash_attn": "sometimes"},
		{"cache_type_k": "q2"},
		{"device": "CUDA0\nLLAMA_ARG_AGENT=1"},
		{"extra_env": "PATH=/tmp"},
		{"extra_env": "LLAMA_ARG_HOST=0.0.0.0"},
		{"extra_env": "LLAMA_API_KEY=secret"},
		{"extra_env": "LLAMA_ARG_TOOLS_RUNTIME=docker:alpine"},
		{"extra_env": "LLAMA_ARG_MCP_SERVERS_JSON={}"},
		{"extra_env": "LLAMA_ARG_MODELS_PRESET=/etc/presets.ini"},
	} {
		if _, err := SaveLlamaConfig(refused); err == nil {
			t.Fatalf("accepted %v", refused)
		}
	}
	config, err := SaveLlamaConfig(map[string]any{"ctx_size": float64(8192), "reasoning_budget": float64(0), "fit": false, "gpu_layers": "all", "extra_env": "GGML_VK_VISIBLE_DEVICES=0\nLLAMA_ARG_BATCH=1024"})
	if err != nil {
		t.Fatal(err)
	}
	env := LlamaManagedEnv(config)
	for name, want := range map[string]string{"LLAMA_ARG_CTX_SIZE": "8192", "LLAMA_ARG_THINK_BUDGET": "0", "LLAMA_ARG_FIT": "off", "LLAMA_ARG_N_GPU_LAYERS": "all", "LLAMA_ARG_UI": "off", "GGML_VK_VISIBLE_DEVICES": "0", "LLAMA_ARG_BATCH": "1024", "LLAMA_ARG_JINJA": "1"} {
		if env[name] == nil || *env[name] != want {
			t.Fatalf("%s: %v", name, env[name])
		}
	}
	if env["LLAMA_ARG_DEVICE"] != nil || env["LLAMA_CACHE"] != nil {
		t.Fatalf("empty settings are set: %v %v", env["LLAMA_ARG_DEVICE"], env["LLAMA_CACHE"])
	}
	if LoadLlamaConfig()["ctx_size"] != int64(8192) {
		t.Fatal("not saved")
	}
	if warnings := LlamaWarnings(config); len(warnings) != 1 {
		t.Fatalf("warnings: %v", warnings)
	}
	// What the console mustn't set doesn't come in from agent-machine's own environment either.
	started := LlamaEnvironment([]string{"LLAMA_ARG_HOST=0.0.0.0", "LLAMA_API_KEY=x", "LLAMA_ARG_AGENT=1", "HOME=/home/me"}, config)
	joined := strings.Join(started, "\n")
	if strings.Contains(joined, "LLAMA_ARG_HOST") || strings.Contains(joined, "LLAMA_API_KEY") || strings.Contains(joined, "LLAMA_ARG_AGENT") || !strings.Contains(joined, "HOME=/home/me") {
		t.Fatalf("environment: %v", started)
	}
}

func TestLlamaStartsWithItsSettingsServesAndStops(t *testing.T) {
	home := t.TempDir()
	t.Setenv("AI_PROXY_MACHINE_HOME", home)
	t.Setenv("LLAMA_CACHE", "")
	setCommand(t, LlamaEnv, fake(t, "fake_llamacpp.py"))
	manager := &Manager{Machine: on, LlamaPort: freePort(t)}
	if state := manager.Describe()["llamacpp"].(map[string]any); state["found"] != true || state["running"] != false || state["version"] != "b9999" || state["source"] != "found" {
		t.Fatalf("before: %v", state)
	}
	if answer := manager.Handle(Request{ID: "c", Op: "configure", Kind: "llamacpp", Config: map[string]any{"ctx_size": float64(32768), "cache_dir": filepath.Join(home, "models")}}); answer["ok"] != true || answer["llamacpp_settings"] == nil {
		t.Fatalf("configure: %v", answer)
	}
	answer := manager.Handle(Request{ID: "s", Op: "start", Kind: "llamacpp"})
	if answer["ok"] != true {
		t.Fatalf("start: %v", answer)
	}
	defer manager.Handle(Request{ID: "x", Op: "stop", Kind: "llamacpp"})
	state := answer["servers"].(map[string]any)["llamacpp"].(map[string]any)
	if state["running"] != true || state["managed"] != true {
		t.Fatalf("after start: %v", state)
	}
	response, err := settings.Client(5 * time.Second).Get(fmt.Sprintf("http://127.0.0.1:%d/props", manager.llamaPort()))
	if err != nil {
		t.Fatal(err)
	}
	var props struct {
		Env  map[string]string `json:"env"`
		Argv []string          `json:"argv"`
	}
	_ = json.NewDecoder(response.Body).Decode(&props)
	response.Body.Close()
	if props.Env["LLAMA_ARG_CTX_SIZE"] != "32768" || props.Env["LLAMA_CACHE"] != filepath.Join(home, "models") || !strings.Contains(strings.Join(props.Argv, " "), "--host 127.0.0.1") {
		t.Fatalf("started with %v", props)
	}
	if settingsAnswer := manager.Handle(Request{ID: "g", Op: "settings", Kind: "llamacpp"}); settingsAnswer["llamacpp_settings"].(map[string]any)["applied_matches_saved"] != true {
		t.Fatalf("settings: %v", settingsAnswer)
	}
	// Ollama's start and stop leave what agent-machine knows about llama-server alone.
	_ = withOllama(0, "", nil, false).save()
	if loadState().LlamaPID == 0 {
		t.Fatal("Ollama's state wiped llama-server's")
	}
	if answer := manager.Handle(Request{ID: "r", Op: "restart", Kind: "llamacpp"}); answer["ok"] != true {
		t.Fatalf("restart: %v", answer)
	}
	if answer := manager.Handle(Request{ID: "t", Op: "stop", Kind: "llamacpp"}); answer["ok"] != true || answer["servers"].(map[string]any)["llamacpp"].(map[string]any)["running"] != false {
		t.Fatalf("stop: %v", answer)
	}
	if text, _ := os.ReadFile(llamaLog()); !strings.Contains(string(text), "LLAMA_ARG_CTX_SIZE=32768") {
		t.Fatalf("log: %s", text)
	}
}

func TestALlamaServerThatFailsToStartSaysWhy(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	setCommand(t, LlamaEnv, fake(t, "fake_llamacpp.py"))
	t.Setenv("FAKE_LLAMACPP_FAIL", "1")
	manager := &Manager{Machine: on, LlamaPort: freePort(t)}
	answer := manager.Handle(Request{ID: "s", Op: "start", Kind: "llamacpp"})
	if answer["ok"] != false || !strings.Contains(answer["error"].(string), "couldn't bind") {
		t.Fatalf("%v", answer)
	}
}

func TestTheBuildIsReadFromEitherVersionLine(t *testing.T) {
	for output, want := range map[string]string{
		"version: 0.8.0-dev (build 12032, commit dde879bc4)\nbuilt with Clang": "b12032",
		"version: 7031 (abc1234)\nbuilt with cc":                               "b7031",
		"something else":                                                       "",
	} {
		if got := parseVersion([]byte(output)); got != want {
			t.Errorf("%q: %q, want %q", output, got, want)
		}
	}
	// Termux's packages: the old ones named the build, the new ones the release.
	for text, want := range map[string]string{"0.0.0-b9590\n": "b9590", "0.0.0-b10516-1": "b10516", "0.8.0": "0.8.0", "1:0.8.0-2": "0.8.0", "": ""} {
		if got := packageVersion(text); got != want {
			t.Errorf("%q: %q, want %q", text, got, want)
		}
	}
	if label, _ := variantLabel("sycl", "windows"); label != "SYCL (Intel)" {
		t.Errorf("%q", label)
	}
}

func TestReleaseAssetsAreMatchedToThisComputer(t *testing.T) {
	cases := []struct {
		platform, goos, goarch, want string
	}{
		{"ubuntu-x64", "linux", "amd64", "cpu"},
		{"ubuntu-vulkan-x64", "linux", "amd64", "vulkan"},
		{"ubuntu-cuda-12.8-x64", "linux", "amd64", "cuda-12.8"},
		{"ubuntu-cuda-13.4-arm64", "linux", "arm64", "cuda-13.4"},
		{"linux-arm64-snapdragon", "linux", "arm64", "snapdragon"},
		{"ubuntu-26.04-s390x", "linux", "amd64", ""},
		{"ubuntu-x64", "linux", "arm64", ""},
		{"win-cpu-x64", "windows", "amd64", "cpu"},
		{"win-cuda-12.4-x64", "windows", "amd64", "cuda-12.4"},
		{"win-opencl-adreno-arm64", "windows", "arm64", "opencl-adreno"},
		{"macos-arm64", "darwin", "arm64", "cpu"},
		{"android-arm64", "linux", "arm64", ""},
		{"win-cpu-x64", "linux", "amd64", ""},
	}
	for _, c := range cases {
		if got := variantOf(c.platform, c.goos, c.goarch); got != c.want {
			t.Errorf("%s on %s/%s: %q, want %q", c.platform, c.goos, c.goarch, got, c.want)
		}
	}
	found := release{Tag: "b100", Assets: []asset{
		{Name: "llama-b100-bin-win-cuda-12.4-x64.zip", Size: 10},
		{Name: "cudart-llama-bin-win-cuda-12.4-x64.zip", Size: 5},
		{Name: "llama-b100-bin-win-cpu-x64.zip", Size: 2},
		{Name: "llama-b100-bin-win-vulkan-x64.zip", Size: 3},
		{Name: "llama-b99-bin-win-hip-x64.zip", Size: 3},
		{Name: "llama-b100-xcframework.zip", Size: 3},
	}}
	variants := releaseVariants(found, "windows", "amd64")
	ids := []string{}
	for _, variant := range variants {
		ids = append(ids, variant.ID)
		if variant.ID == "cuda-12.4" && (variant.runtime == nil || variant.Size != 15) {
			t.Fatalf("CUDA without its runtime: %+v", variant)
		}
	}
	if strings.Join(ids, ",") != "cpu,cuda-12.4,vulkan" {
		t.Fatalf("variants %v", ids)
	}
}

// fakeGitHub serves a releases list with one build for this computer, and its archive.
func fakeGitHub(t *testing.T, archive []byte, digest string) *httptest.Server {
	t.Helper()
	systems, arch := platformTokens(runtime.GOOS, runtime.GOARCH)
	if systems == nil || arch == "" {
		t.Skip("llama.cpp publishes no build for this computer")
	}
	extension := ".tar.gz"
	if runtime.GOOS == "windows" {
		extension = ".zip"
	}
	name := "llama-b100-bin-" + systems[0] + "-" + arch + extension
	if runtime.GOOS == "windows" {
		name = "llama-b100-bin-win-cpu-" + arch + extension
	}
	mux := http.NewServeMux()
	var server *httptest.Server
	mux.HandleFunc("/releases", func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode([]map[string]any{
			{"tag_name": "b101", "assets": []any{}},
			{"tag_name": "b100", "prerelease": true, "assets": []any{map[string]any{"name": name, "size": len(archive), "digest": digest, "browser_download_url": server.URL + "/download"}}},
		})
	})
	mux.HandleFunc("/download", func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write(archive) })
	server = httptest.NewServer(mux)
	t.Cleanup(server.Close)
	t.Setenv(ReleasesEnv, server.URL+"/releases")
	return server
}

func buildArchive(t *testing.T, files map[string]string) []byte {
	t.Helper()
	var buffer bytes.Buffer
	if runtime.GOOS == "windows" {
		writer := zip.NewWriter(&buffer)
		for name, body := range files {
			entry, _ := writer.Create(name)
			_, _ = entry.Write([]byte(body))
		}
		writer.Close()
		return buffer.Bytes()
	}
	compressed := gzip.NewWriter(&buffer)
	writer := tar.NewWriter(compressed)
	for name, body := range files {
		_ = writer.WriteHeader(&tar.Header{Name: name, Mode: 0o755, Size: int64(len(body)), Typeflag: tar.TypeReg})
		_, _ = writer.Write([]byte(body))
	}
	writer.Close()
	compressed.Close()
	return buffer.Bytes()
}

func sha(data []byte) string {
	sum := sha256.Sum256(data)
	return "sha256:" + hex.EncodeToString(sum[:])
}

func waitInstall(t *testing.T) installJob {
	t.Helper()
	for deadline := time.Now().Add(20 * time.Second); time.Now().Before(deadline); time.Sleep(50 * time.Millisecond) {
		if progress, ok := installProgress().(installJob); ok && progress.State != "installing" {
			return progress
		}
	}
	t.Fatal("the install didn't finish")
	return installJob{}
}

func TestInstallingFromGitHubChecksTheDownload(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	t.Setenv("PREFIX", "")
	t.Setenv("TERMUX_VERSION", "")
	t.Setenv(LlamaEnv, "")
	archive := buildArchive(t, map[string]string{"llama-b100/" + serverName(): "#!/bin/sh\n", "llama-b100/libggml-cpu.so": "lib"})
	fakeGitHub(t, archive, "sha256:"+strings.Repeat("0", 64))
	manager := &Manager{Machine: on, LlamaPort: freePort(t)}

	options := manager.Handle(Request{ID: "o", Op: "install_options", Kind: "llamacpp"})["llamacpp_install"].(map[string]any)
	if options["release"] != "b100" || len(options["variants"].([]Variant)) != 1 || !options["variants"].([]Variant)[0].Recommended {
		t.Fatalf("options: %v", options)
	}
	if answer := manager.Handle(Request{ID: "i", Op: "install", Kind: "llamacpp", Variant: "cpu; rm -rf /"}); answer["ok"] != false {
		t.Fatalf("a made-up variant was accepted: %v", answer)
	}
	if answer := manager.Handle(Request{ID: "i", Op: "install", Kind: "llamacpp", Variant: "cpu"}); answer["ok"] != true {
		t.Fatalf("install: %v", answer)
	}
	if done := waitInstall(t); done.State != "failed" || !strings.Contains(done.Error, "isn't what GitHub lists") {
		t.Fatalf("a download that doesn't match its digest was installed: %+v", done)
	}
	if loadInstall().Binary != "" {
		t.Fatal("recorded anyway")
	}

	fakeGitHub(t, archive, sha(archive))
	if answer := manager.Handle(Request{ID: "i2", Op: "install", Kind: "llamacpp", Variant: "cpu"}); answer["ok"] != true {
		t.Fatalf("install: %v", answer)
	}
	if done := waitInstall(t); done.State != "installed" || done.Release != "b100" {
		t.Fatalf("install: %+v", done)
	}
	record := loadInstall()
	if record.Source != "github" || record.Variant != "cpu" || !strings.HasPrefix(record.Binary, installRoot()) {
		t.Fatalf("record %+v", record)
	}
	if program := llamaCommand(); program == nil || program[0] != record.Binary {
		t.Fatalf("runs %v, not what was installed", program)
	}
	state := manager.Describe()["llamacpp"].(map[string]any)
	if state["source"] != "github" || state["release"] != "b100" || state["variant"] != "cpu" {
		t.Fatalf("state %v", state)
	}
	if answer := manager.Handle(Request{ID: "u", Op: "uninstall", Kind: "llamacpp"}); answer["ok"] != true || llamaCommand() != nil && llamaCommand()[0] == record.Binary {
		t.Fatalf("uninstall: %v", answer)
	}
	if _, err := os.Stat(installRoot()); !os.IsNotExist(err) {
		t.Fatalf("left %s", installRoot())
	}
}

func TestArchivesCantWriteOutsideTheirFolder(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	t.Setenv("PREFIX", "")
	t.Setenv("TERMUX_VERSION", "")
	archive := buildArchive(t, map[string]string{"../../escaped": "x", "llama/" + serverName(): "x"})
	fakeGitHub(t, archive, sha(archive))
	if err := startInstall("cpu"); err != nil {
		t.Fatal(err)
	}
	if done := waitInstall(t); done.State != "failed" || !strings.Contains(done.Error, "outside the archive") {
		t.Fatalf("%+v", done)
	}
	if _, err := os.Stat(filepath.Join(settings.Home(), "..", "escaped")); err == nil {
		t.Fatal("an entry escaped the install folder")
	}
}

func TestInstallingInTermuxUsesItsPackages(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Termux is Android's")
	}
	prefix := t.TempDir()
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	t.Setenv("PREFIX", prefix)
	t.Setenv("TERMUX_VERSION", "0.118")
	// The fake apt-get notes what it was asked and installs llama-server on install.
	script := filepath.Join(t.TempDir(), "apt-get")
	calls := filepath.Join(t.TempDir(), "calls")
	_ = os.WriteFile(script, []byte(fmt.Sprintf("#!/bin/sh\necho \"$@\" >> %q\nif [ \"$1\" = install ]; then mkdir -p %q && touch %q; fi\n", calls, filepath.Join(prefix, "bin"), filepath.Join(prefix, "bin", "llama-server"))), 0o700)
	setCommand(t, AptEnv, []string{"/bin/sh", script})
	manager := &Manager{Machine: on, LlamaPort: freePort(t)}
	options := manager.Handle(Request{ID: "o", Op: "install_options", Kind: "llamacpp"})["llamacpp_install"].(map[string]any)
	if options["source"] != "termux" || options["variants"].([]Variant)[0].ID != "opencl" {
		t.Fatalf("options %v", options)
	}
	if answer := manager.Handle(Request{ID: "i", Op: "install", Kind: "llamacpp", Variant: "metal"}); answer["ok"] != true {
		t.Fatalf("%v", answer)
	}
	if done := waitInstall(t); done.State != "failed" {
		t.Fatalf("Termux has no metal build: %+v", done)
	}
	if answer := manager.Handle(Request{ID: "i", Op: "install", Kind: "llamacpp", Variant: "opencl"}); answer["ok"] != true {
		t.Fatalf("%v", answer)
	}
	if done := waitInstall(t); done.State != "installed" {
		t.Fatalf("%+v", done)
	}
	asked, _ := os.ReadFile(calls)
	if !strings.Contains(string(asked), "update") || !strings.Contains(string(asked), "install -y -o Dpkg::Options::=--force-confnew llama-cpp llama-cpp-backend-opencl ocl-icd opencl-vendor-driver") {
		t.Fatalf("apt-get was asked: %s", asked)
	}
	if record := loadInstall(); record.Source != "termux" || record.Binary != filepath.Join(prefix, "bin", "llama-server") {
		t.Fatalf("%+v", record)
	}
	if answer := manager.Handle(Request{ID: "u", Op: "uninstall", Kind: "llamacpp"}); answer["ok"] != false {
		t.Fatalf("removed Termux's packages: %v", answer)
	}
}
