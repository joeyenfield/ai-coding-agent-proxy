package mcp

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"
)

// The test binary plays a stdio MCP server when MCP_FAKE_SERVER is set: it answers initialize and
// tools/list, and says a progress notification before answering tools/call.
func TestMain(m *testing.M) {
	if os.Getenv("MCP_FAKE_SERVER") == "1" {
		fakeServer()
		return
	}
	os.Exit(m.Run())
}

func fakeServer() {
	scanner := bufio.NewScanner(os.Stdin)
	for scanner.Scan() {
		var message map[string]any
		if json.Unmarshal(scanner.Bytes(), &message) != nil || message["id"] == nil {
			continue
		}
		reply := map[string]any{"jsonrpc": "2.0", "id": message["id"]}
		switch message["method"] {
		case "initialize":
			reply["result"] = map[string]any{"protocolVersion": "2025-06-18", "capabilities": map[string]any{"tools": map[string]any{}}, "serverInfo": map[string]any{"name": "fake"}}
		case "tools/list":
			reply["result"] = map[string]any{"tools": []any{map[string]any{"name": "echo", "inputSchema": map[string]any{"type": "object"}}}}
		case "tools/call":
			fmt.Println(`{"jsonrpc":"2.0","method":"notifications/progress","params":{"progress":1}}`)
			reply["result"] = map[string]any{"content": []any{map[string]any{"type": "text", "text": "echoed " + os.Getenv("FAKE_GREETING")}}}
		default:
			reply["error"] = map[string]any{"code": -32601, "message": "no such method"}
		}
		data, _ := json.Marshal(reply)
		fmt.Println(string(data))
	}
}

func testBridge(t *testing.T) (*Bridge, *httptest.Server, string) {
	t.Helper()
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("MCP_FAKE_SERVER", "1")
	servers := []Server{
		{Name: "fake", Command: []string{executable}, Env: map[string]string{"FAKE_GREETING": "hello"}},
	}
	bridge := &Bridge{Load: func() ([]Server, error) { return servers, nil }}
	_, key, err := bridge.Start()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(bridge.Close)
	server := httptest.NewServer(bridge)
	t.Cleanup(server.Close)
	return bridge, server, key
}

func call(t *testing.T, method, url, key, session, body string, accept string) *http.Response {
	t.Helper()
	request, _ := http.NewRequest(method, url, strings.NewReader(body))
	if key != "" {
		request.Header.Set(KeyHeader, key)
	}
	if session != "" {
		request.Header.Set(sessionHeader, session)
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", accept)
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	return response
}

func events(t *testing.T, response *http.Response) []map[string]any {
	t.Helper()
	defer response.Body.Close()
	var found []map[string]any
	scanner := bufio.NewScanner(response.Body)
	for scanner.Scan() {
		if data, ok := strings.CutPrefix(scanner.Text(), "data: "); ok {
			var message map[string]any
			if err := json.Unmarshal([]byte(data), &message); err != nil {
				t.Fatalf("bad event %q", data)
			}
			found = append(found, message)
		}
	}
	return found
}

const both = "application/json, text/event-stream"

func TestAStdioServerIsAnMCPSessionOverStreamableHTTP(t *testing.T) {
	_, server, key := testBridge(t)
	url := server.URL + "/mcp/fake"

	response := call(t, "POST", url, key, "", `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}`, both)
	session := response.Header.Get(sessionHeader)
	if response.StatusCode != 200 || session == "" || !strings.HasPrefix(response.Header.Get("Content-Type"), "text/event-stream") {
		t.Fatalf("initialize answered %d, session %q", response.StatusCode, session)
	}
	if found := events(t, response); len(found) != 1 || found[0]["result"].(map[string]any)["serverInfo"] == nil {
		t.Fatalf("initialize: %v", found)
	}

	response = call(t, "POST", url, key, session, `{"jsonrpc":"2.0","method":"notifications/initialized"}`, both)
	response.Body.Close()
	if response.StatusCode != http.StatusAccepted {
		t.Fatalf("a notification answered %d", response.StatusCode)
	}

	// The notification it says while working comes on the same stream, before the answer.
	found := events(t, call(t, "POST", url, key, session, `{"jsonrpc":"2.0","id":"call-1","method":"tools/call","params":{"name":"echo"}}`, both))
	if len(found) != 2 || found[0]["method"] != "notifications/progress" || found[1]["id"] != "call-1" {
		t.Fatalf("tools/call: %v", found)
	}
	if text := found[1]["result"].(map[string]any)["content"].([]any)[0].(map[string]any)["text"]; text != "echoed hello" {
		t.Fatalf("the server's environment didn't reach it: %v", text)
	}

	// A client that only takes JSON gets the answer alone.
	response = call(t, "POST", url, key, session, `{"jsonrpc":"2.0","id":7,"method":"tools/list"}`, "application/json")
	body, _ := io.ReadAll(response.Body)
	response.Body.Close()
	var listed map[string]any
	if json.Unmarshal(body, &listed) != nil || listed["id"] != float64(7) {
		t.Fatalf("tools/list as JSON: %s", body)
	}

	response = call(t, "DELETE", url, key, session, "", both)
	response.Body.Close()
	deadline := time.Now().Add(5 * time.Second)
	for {
		response = call(t, "POST", url, key, session, `{"jsonrpc":"2.0","id":8,"method":"tools/list"}`, both)
		response.Body.Close()
		if response.StatusCode == http.StatusNotFound {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("an ended session answered %d", response.StatusCode)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func TestTheBridgeWantsItsKeyAndAServerItShares(t *testing.T) {
	_, server, key := testBridge(t)
	initialize := `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}`
	for _, test := range []struct {
		key, path string
		status    int
	}{
		{"", "/mcp/fake", http.StatusUnauthorized},
		{"wrong", "/mcp/fake", http.StatusUnauthorized},
		{key, "/mcp/other", http.StatusNotFound},
		{key, "/elsewhere", http.StatusNotFound},
	} {
		response := call(t, "POST", server.URL+test.path, test.key, "", initialize, both)
		response.Body.Close()
		if response.StatusCode != test.status {
			t.Errorf("key %q, %s: %d, want %d", test.key, test.path, response.StatusCode, test.status)
		}
	}
	// Anything but initialize needs a session.
	response := call(t, "POST", server.URL+"/mcp/fake", key, "", `{"jsonrpc":"2.0","id":2,"method":"tools/list"}`, both)
	response.Body.Close()
	if response.StatusCode != http.StatusBadRequest {
		t.Errorf("tools/list without a session: %d", response.StatusCode)
	}
}

func TestAURLServerIsProxiedWithItsHeadersAndWithoutTheKey(t *testing.T) {
	var seen http.Header
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r.Header.Clone()
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set(sessionHeader, "upstream-session")
		_, _ = io.WriteString(w, `{"jsonrpc":"2.0","id":1,"result":{"path":"`+r.URL.Path+`"}}`)
	}))
	defer upstream.Close()
	bridge := &Bridge{Load: func() ([]Server, error) {
		return []Server{{Name: "remote", URL: upstream.URL + "/v1/mcp", Headers: map[string]string{"Authorization": "Bearer secret"}}}, nil
	}}
	_, key, err := bridge.Start()
	if err != nil {
		t.Fatal(err)
	}
	defer bridge.Close()
	server := httptest.NewServer(bridge)
	defer server.Close()
	response := call(t, "POST", server.URL+"/mcp/remote", key, "", `{"jsonrpc":"2.0","id":1,"method":"initialize"}`, both)
	body, _ := io.ReadAll(response.Body)
	response.Body.Close()
	if !strings.Contains(string(body), `"/v1/mcp"`) || response.Header.Get(sessionHeader) != "upstream-session" {
		t.Fatalf("proxied: %d %s", response.StatusCode, body)
	}
	if seen.Get(KeyHeader) != "" || seen.Get("Authorization") != "Bearer secret" {
		t.Fatalf("upstream saw key %q, authorization %q", seen.Get(KeyHeader), seen.Get("Authorization"))
	}
}

func TestDescribeNamesServersButNotHowTheyRun(t *testing.T) {
	bridge := &Bridge{Load: func() ([]Server, error) {
		return []Server{{Name: "gh", Command: []string{"definitely-not-installed-xyz"}, Env: map[string]string{"GITHUB_TOKEN": "secret"}}}, nil
	}}
	defer bridge.Close()
	described := bridge.Describe()
	data, _ := json.Marshal(described)
	if strings.Contains(string(data), "secret") || strings.Contains(string(data), "definitely-not") {
		t.Fatalf("the hello gives away the server's settings: %s", data)
	}
	if described["port"].(int) == 0 || described["key"] == "" || !strings.Contains(string(data), `"found":false`) {
		t.Fatalf("described: %s", data)
	}
	empty := &Bridge{Load: func() ([]Server, error) { return nil, nil }}
	if empty.Describe() != nil || empty.Port() != 0 {
		t.Fatal("a machine with no MCP servers shouldn't open a port")
	}
}

func TestServerSettingsAreChecked(t *testing.T) {
	for _, server := range []Server{
		{Name: "Bad Name", Command: []string{"x"}},
		{Name: "both", Command: []string{"x"}, URL: "http://127.0.0.1:1/mcp"},
		{Name: "neither"},
		{Name: "ftp", URL: "ftp://example.com"},
	} {
		if server.Check() == nil {
			t.Errorf("%+v passed", server)
		}
	}
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	if err := Add(Server{Name: "one", Command: []string{"npx", "server"}}); err != nil {
		t.Fatal(err)
	}
	if err := Add(Server{Name: "one", URL: "http://127.0.0.1:3000/mcp"}); err != nil {
		t.Fatal(err)
	}
	servers, _ := Load()
	if len(servers) != 1 || servers[0].Transport() != "http" {
		t.Fatalf("replaced: %+v", servers)
	}
	if removed, _ := Remove("one"); !removed {
		t.Fatal("not removed")
	}
	if info, err := os.Stat(Path()); err != nil || (info.Mode().Perm()&0o077 != 0 && os.PathSeparator == '/') {
		t.Fatalf("mcp.json: %v %v", info, err)
	}
}
