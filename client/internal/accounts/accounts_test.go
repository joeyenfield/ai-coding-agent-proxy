package accounts

import (
	"errors"
	"path/filepath"
	"strings"
	"testing"
)

type fake struct {
	paths  map[string]string
	output map[string]struct {
		text   string
		status int
	}
	env   map[string]string
	files map[string]bool
	ran   []string
}

func (f *fake) system() System {
	return System{
		LookPath: func(name string) (string, error) {
			if path, ok := f.paths[name]; ok {
				return path, nil
			}
			return "", errors.New("not found")
		},
		Run: func(path string, args ...string) (string, int) {
			line := path + " " + strings.Join(args, " ")
			f.ran = append(f.ran, line)
			if answer, ok := f.output[line]; ok {
				return answer.text, answer.status
			}
			return "", 1
		},
		Getenv: func(name string) string { return f.env[name] },
		Exists: func(path string) bool { return f.files[filepath.ToSlash(path)] },
		Home:   "/home/me",
		GOOS:   "linux",
	}
}

func find(report Report, provider string) CLI {
	for _, cli := range report.CLIs {
		if cli.Provider == provider {
			return cli
		}
	}
	return CLI{}
}

func TestDetectReadsNoCredentialsAndReportsEachSignIn(t *testing.T) {
	f := &fake{
		paths: map[string]string{"claude": "/bin/claude", "codex": "/bin/codex", "copilot": "/bin/copilot"},
		output: map[string]struct {
			text   string
			status int
		}{
			"/bin/claude --version":   {"2.1.0 (Claude Code)\n", 0},
			"/bin/codex --version":    {"codex-cli 0.50.0", 0},
			"/bin/codex login status": {"Logged in using ChatGPT\n", 0},
			"/bin/copilot --version":  {"1.0.0", 0},
		},
		env:   map[string]string{},
		files: map[string]bool{"/home/me/.claude/.credentials.json": true},
	}
	report := Detect(f.system())
	claude, codex, copilot := find(report, "claude"), find(report, "codex"), find(report, "copilot")
	if !claude.Installed || claude.Auth != "signed_in" || claude.Method != "claude.ai" || claude.Version != "2.1.0 (Claude Code)" {
		t.Fatalf("claude %+v", claude)
	}
	if codex.Auth != "signed_in" || codex.Method != "chatgpt" || codex.Subscription == nil || !*codex.Subscription {
		t.Fatalf("codex %+v", codex)
	}
	if copilot.Auth != "unknown" || strings.Join(copilot.Modes, ",") != "runner,token" {
		t.Fatalf("copilot %+v", copilot)
	}
	// Only version and status commands run; nothing reads a sign-in.
	for _, line := range f.ran {
		if !strings.HasSuffix(line, "--version") && !strings.HasSuffix(line, "login status") {
			t.Fatalf("ran %s", line)
		}
	}
}

func TestDetectTellsSignedOutAndApiKeysFromSubscriptions(t *testing.T) {
	f := &fake{
		paths: map[string]string{"claude": "/bin/claude", "codex": "/bin/codex", "copilot": "/bin/copilot"},
		output: map[string]struct {
			text   string
			status int
		}{"/bin/codex login status": {"Logged in using an API key - sk-proj-***ABCD", 0}},
		env: map[string]string{"GH_TOKEN": "x"},
	}
	report := Detect(f.system())
	if claude := find(report, "claude"); claude.Auth != "signed_out" || !claude.Reauthenticate {
		t.Fatalf("claude %+v", claude)
	}
	codex := find(report, "codex")
	if codex.Method != "api_key" || codex.Subscription == nil || *codex.Subscription || strings.Contains(codex.Note, "ABCD") {
		t.Fatalf("codex %+v", codex)
	}
	if copilot := find(report, "copilot"); copilot.Auth != "signed_in" || copilot.Method != "token:GH_TOKEN" {
		t.Fatalf("copilot %+v", copilot)
	}
	f.env = map[string]string{"ANTHROPIC_API_KEY": "sk"}
	f.output["/bin/codex login status"] = struct {
		text   string
		status int
	}{"Not logged in", 1}
	report = Detect(f.system())
	if claude := find(report, "claude"); claude.Method != "api_key" || *claude.Subscription {
		t.Fatalf("claude with a key %+v", claude)
	}
	if codex := find(report, "codex"); codex.Auth != "signed_out" {
		t.Fatalf("codex signed out %+v", codex)
	}
	f.paths = map[string]string{}
	if claude := find(Detect(f.system()), "claude"); claude.Installed || claude.Auth != "unknown" {
		t.Fatalf("missing %+v", claude)
	}
}

func TestEnrollArguments(t *testing.T) {
	options, err := parseEnroll([]string{"copilot", "--name=Copilot Pro", "--plan", "Pro", "--token-stdin", "--yes", "--machine", "m1"})
	if err != nil || options.provider != "copilot" || options.name != "Copilot Pro" || options.plan != "Pro" || !options.tokenStdin || !options.yes || options.machine != "m1" {
		t.Fatalf("%+v %v", options, err)
	}
	if _, err := parseEnroll([]string{"claude", "--token-stdin"}); err == nil {
		t.Fatal("a Claude token was accepted")
	}
	for _, args := range [][]string{{}, {"gemini"}, {"codex", "--nope"}, {"codex", "--name"}} {
		if _, err := parseEnroll(args); err == nil {
			t.Fatalf("%v accepted", args)
		}
	}
}
