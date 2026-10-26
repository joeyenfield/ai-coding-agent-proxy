package update

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"ai-coding-agent-proxy/machine/internal/settings"
)

var programs = []string{"agent-machine"}

func serve(t *testing.T, data, checksum string) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("User-Agent"); !strings.HasPrefix(got, "agent-machine/") {
			t.Errorf("unidentified request: User-Agent %q", got)
		}
		if r.URL.Path == "/downloads/index.json" {
			_ = json.NewEncoder(w).Encode(Index{ServerVersion: "0.5.0", Version: "0.5.0", Exact: true, Files: []File{
				{Path: "linux-amd64/agent-machine-0.5.0-linux-amd64.tar.gz", URL: "/nope"},
				{Path: "linux-amd64/artifact/agent-machine", SHA256: checksum, URL: "/downloads/0.5.0/linux-amd64/artifact/agent-machine"},
				{Path: "windows-arm64/artifact/agent-machine.exe", SHA256: checksum, URL: "/downloads/0.5.0/windows-arm64/artifact/agent-machine.exe"},
			}})
			return
		}
		_, _ = w.Write([]byte(data))
	}))
	t.Cleanup(server.Close)
	return server
}

func TestUpdatePicksTheBinaryAndChecksIt(t *testing.T) {
	hash := sha256.Sum256([]byte("new machine"))
	good := hex.EncodeToString(hash[:])
	for _, platform := range []string{"linux-amd64", "windows-arm64"} {
		dir := t.TempDir()
		server := serve(t, "new machine", good)
		index, err := Fetch(server.URL)
		if err != nil {
			t.Fatal(err)
		}
		steps, err := Plan(index, platform, dir, programs)
		if err != nil || len(steps) != 1 || !strings.HasPrefix(steps[0].File.Path, platform+"/artifact/agent-machine") {
			t.Fatalf("%s: %v %+v", platform, err, steps)
		}
		_ = os.WriteFile(steps[0].Target, []byte("old"), 0o755)
		if err := Apply(server.URL, steps); err != nil {
			t.Fatal(err)
		}
		if data, _ := os.ReadFile(steps[0].Target); string(data) != "new machine" {
			t.Fatalf("%s: %q", platform, data)
		}
		Cleanup(dir, programs)
		if left, _ := filepath.Glob(filepath.Join(dir, "*.old*")); len(left) != 0 {
			t.Errorf("left: %v", left)
		}
	}
	dir := t.TempDir()
	server := serve(t, "tampered", good)
	index, _ := Fetch(server.URL)
	steps, _ := Plan(index, "linux-amd64", dir, programs)
	_ = os.WriteFile(steps[0].Target, []byte("old"), 0o755)
	if err := Apply(server.URL, steps); err == nil || !strings.Contains(err.Error(), "checksum") {
		t.Fatalf("tampered: %v", err)
	}
	if data, _ := os.ReadFile(steps[0].Target); string(data) != "old" {
		t.Fatalf("replaced anyway: %q", data)
	}
	if _, err := Plan(index, "darwin-arm64", dir, programs); err == nil {
		t.Fatal("no build for this platform, yet a plan")
	}
	if settings.Platform() == "" {
		t.Fatal("no platform")
	}
}
