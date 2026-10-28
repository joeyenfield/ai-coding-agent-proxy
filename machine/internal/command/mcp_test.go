package command

import (
	"testing"

	"ai-coding-agent-proxy/machine/internal/mcp"
)

func TestMCPAddTakesACommandOrAURL(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	if status := Main([]string{"mcp", "add", "github", "--env", "GITHUB_TOKEN=abc=def", "--description=GitHub", "--", "npx", "-y", "@modelcontextprotocol/server-github"}); status != 0 {
		t.Fatalf("add a command: %d", status)
	}
	if status := Main([]string{"mcp", "add", "docs", "--url", "http://127.0.0.1:3000/mcp", "--header", "Authorization: Bearer x"}); status != 0 {
		t.Fatalf("add a URL: %d", status)
	}
	servers, _ := mcp.Load()
	if len(servers) != 2 {
		t.Fatalf("servers: %+v", servers)
	}
	docs, github := servers[0], servers[1]
	if github.Env["GITHUB_TOKEN"] != "abc=def" || github.Description != "GitHub" || len(github.Command) != 3 || github.Command[1] != "-y" {
		t.Fatalf("github: %+v", github)
	}
	if docs.URL != "http://127.0.0.1:3000/mcp" || docs.Headers["Authorization"] != "Bearer x" {
		t.Fatalf("docs: %+v", docs)
	}
	for _, args := range [][]string{
		{"mcp", "add", "Bad", "--", "x"},
		{"mcp", "add", "none"},
		{"mcp", "add", "x", "--url"},
		{"mcp", "add", "x", "--unknown", "1", "--", "x"},
		{"mcp", "remove", "missing"},
	} {
		if Main(args) == 0 {
			t.Errorf("%v passed", args)
		}
	}
	if Main([]string{"mcp", "remove", "docs"}) != 0 {
		t.Fatal("remove failed")
	}
	if servers, _ := mcp.Load(); len(servers) != 1 {
		t.Fatalf("after remove: %+v", servers)
	}
}
