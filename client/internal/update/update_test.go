package update

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"ai-coding-agent-proxy/client/internal/settings"
)

func sum(data string) string {
	hash := sha256.Sum256([]byte(data))
	return hex.EncodeToString(hash[:])
}

// downloads plays the server's /downloads: an index of files, and the files themselves.
func downloads(t *testing.T, version string, files map[string]string, sums map[string]string) (*httptest.Server, *[]http.Header) {
	t.Helper()
	var seen []http.Header
	var server *httptest.Server
	server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.Header.Clone())
		if r.URL.Path == "/downloads/index.json" {
			index := Index{ServerVersion: version, Version: version, Exact: true}
			for path, data := range files {
				checksum := sum(data)
				if override, ok := sums[path]; ok {
					checksum = override
				}
				index.Files = append(index.Files, File{Path: path, Name: filepath.Base(path), Size: int64(len(data)), SHA256: checksum, URL: "/downloads/" + version + "/" + path})
			}
			_ = json.NewEncoder(w).Encode(index)
			return
		}
		path := strings.TrimPrefix(r.URL.Path, "/downloads/"+version+"/")
		data, ok := files[path]
		if !ok {
			http.NotFound(w, r)
			return
		}
		_, _ = w.Write([]byte(data))
	}))
	t.Cleanup(server.Close)
	return server, &seen
}

func TestPlanPicksThisPlatformsBinaries(t *testing.T) {
	index := &Index{Version: "0.5.0", Files: []File{
		{Path: "linux-amd64/agent-0.5.0-linux-amd64.tar.gz"},
		{Path: "linux-amd64/artifact/agent"}, {Path: "linux-amd64/artifact/agent-proxy"},
		{Path: "linux-arm64/artifact/agent"}, {Path: "linux-arm64/artifact/agent-proxy"},
		{Path: "windows-amd64/artifact/agent.exe"}, {Path: "windows-amd64/artifact/agent-proxy.exe"},
	}}
	steps, err := Plan(index, "windows-amd64", `C:\tools`, Programs)
	if err != nil || len(steps) != 2 || steps[0].File.Path != "windows-amd64/artifact/agent.exe" || steps[1].Target != filepath.Join(`C:\tools`, "agent-proxy.exe") {
		t.Fatalf("windows: %v %+v", err, steps)
	}
	steps, err = Plan(index, "linux-arm64", "/usr/local/bin", Programs)
	if err != nil || steps[0].File.Path != "linux-arm64/artifact/agent" || steps[1].File.Path != "linux-arm64/artifact/agent-proxy" {
		t.Fatalf("linux: %v %+v", err, steps)
	}
	if _, err := Plan(index, "darwin-arm64", "/usr/local/bin", Programs); err == nil || !strings.Contains(err.Error(), "darwin-arm64") {
		t.Fatalf("a platform the server didn't build: %v", err)
	}
	if _, err := Plan(&Index{ServerVersion: "0.5.0", Hint: "run scripts/build-dist.sh"}, "linux-amd64", "/x", Programs); err == nil || !strings.Contains(err.Error(), "build-dist") {
		t.Fatalf("no builds: %v", err)
	}
	if settings.Platform() == "" || strings.Contains(settings.Platform(), "/") {
		t.Fatalf("platform %q", settings.Platform())
	}
}

func TestUpdateReplacesBothBinariesFromTheServer(t *testing.T) {
	settings.Version = "0.4.0"
	t.Cleanup(func() { settings.Version = "dev" })
	platform := "linux-amd64"
	server, seen := downloads(t, "0.5.0", map[string]string{platform + "/artifact/agent": "new agent", platform + "/artifact/agent-proxy": "new proxy"}, nil)
	dir := t.TempDir()
	for _, name := range []string{"agent", "agent-proxy"} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte("old "+name), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	index, err := Fetch(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	steps, err := Plan(index, platform, dir, Programs)
	if err != nil {
		t.Fatal(err)
	}
	if err := Apply(server.URL, steps); err != nil {
		t.Fatal(err)
	}
	for name, want := range map[string]string{"agent": "new agent", "agent-proxy": "new proxy"} {
		data, _ := os.ReadFile(filepath.Join(dir, name))
		if string(data) != want {
			t.Errorf("%s: %q", name, data)
		}
		_, err := os.Stat(filepath.Join(dir, name+".old"))
		// Windows keeps the old binary (it may be running) until Cleanup; elsewhere it's gone at once.
		if kept := err == nil; kept != (runtime.GOOS == "windows") {
			t.Errorf("%s.old kept: %v", name, kept)
		}
		if runtime.GOOS != "windows" {
			if info, _ := os.Stat(filepath.Join(dir, name)); info.Mode()&0o111 == 0 {
				t.Errorf("%s isn't executable: %v", name, info.Mode())
			}
		}
	}
	Cleanup(dir, Programs)
	if left, _ := filepath.Glob(filepath.Join(dir, "*.old*")); len(left) != 0 {
		t.Errorf("left after cleanup: %v", left)
	}
	// The requests say which client asks, like every request to the server.
	for _, header := range *seen {
		if header.Get(settings.VersionHeader) != "0.4.0" || header.Get("User-Agent") != "agent/0.4.0" {
			t.Errorf("unidentified request: %v", header)
		}
	}
}

func TestAChecksumMismatchReplacesNothing(t *testing.T) {
	platform := "linux-amd64"
	server, _ := downloads(t, "0.5.0",
		map[string]string{platform + "/artifact/agent": "new agent", platform + "/artifact/agent-proxy": "tampered"},
		map[string]string{platform + "/artifact/agent-proxy": sum("what was built")})
	dir := t.TempDir()
	for _, name := range []string{"agent", "agent-proxy"} {
		_ = os.WriteFile(filepath.Join(dir, name), []byte("old "+name), 0o755)
	}
	index, _ := Fetch(server.URL)
	steps, _ := Plan(index, platform, dir, Programs)
	err := Apply(server.URL, steps)
	if err == nil || !strings.Contains(err.Error(), "checksum") {
		t.Fatalf("got %v", err)
	}
	for _, name := range []string{"agent", "agent-proxy"} {
		if data, _ := os.ReadFile(filepath.Join(dir, name)); string(data) != "old "+name {
			t.Errorf("%s replaced: %q", name, data)
		}
	}
	if left, _ := filepath.Glob(filepath.Join(dir, ".*new*")); len(left) != 0 {
		t.Errorf("downloads left behind: %v", left)
	}
}

func TestSwapInstallsAMissingBinaryAndKeepsABusyOld(t *testing.T) {
	dir := t.TempDir()
	fresh := filepath.Join(dir, "fresh")
	_ = os.WriteFile(fresh, []byte("v2"), 0o755)
	target := filepath.Join(dir, "agent-proxy")
	if err := Swap(fresh, target); err != nil {
		t.Fatal(err)
	}
	if data, _ := os.ReadFile(target); string(data) != "v2" {
		t.Fatalf("got %q", data)
	}
	// A .old that can't be removed (still running on Windows; a folder stands in for it) gets another name.
	_ = os.Mkdir(target+".old", 0o755)
	_ = os.WriteFile(filepath.Join(target+".old", "busy"), []byte("x"), 0o644)
	_ = os.WriteFile(fresh, []byte("v3"), 0o755)
	if err := Swap(fresh, target); err != nil {
		t.Fatal(err)
	}
	if data, _ := os.ReadFile(target); string(data) != "v3" {
		t.Fatalf("got %q", data)
	}
}

func TestAnOldServerWithoutDownloadsSaysSo(t *testing.T) {
	server := httptest.NewServer(http.NotFoundHandler())
	t.Cleanup(server.Close)
	if _, err := Fetch(server.URL); err == nil || !strings.Contains(err.Error(), "doesn't offer downloads") {
		t.Fatalf("got %v", err)
	}
}
