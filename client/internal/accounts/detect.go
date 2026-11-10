// Package accounts is `agent accounts`: the AI subscriptions (provider accounts) of the user this
// computer is signed in as, the provider CLIs on this computer, and registering it as a machine
// a subscription runs on.
//
// Detecting a sign-in never reads a credential: it runs the CLI's own status command where the
// provider documents one (`codex login status`), or checks that a sign-in file exists without
// opening it (Claude Code's .credentials.json), and otherwise says it can't tell. agent-machine
// runs `agent accounts detect --json` and reports the result in its hello: booleans and versions.
package accounts

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"ai-control-mesh/client/internal/process"
)

// Providers are the kinds of subscription, in the server's order (provider_accounts.PROVIDERS).
var Providers = []string{"claude", "codex", "copilot"}

// CLI is what this computer has of one provider: its CLI, and whether that's signed in.
type CLI struct {
	Provider  string `json:"provider"`
	Installed bool   `json:"installed"`
	Version   string `json:"version,omitempty"`
	// Auth is signed_in, signed_out or unknown.
	Auth   string `json:"auth"`
	Method string `json:"method,omitempty"`
	// Subscription is false when it's signed in, but with an API key rather than a subscription.
	Subscription *bool `json:"subscription,omitempty"`
	// Modes are how an account of this provider can sign in here (runner, token).
	Modes          []string `json:"modes"`
	Reauthenticate bool     `json:"reauthenticate"`
	Note           string   `json:"note,omitempty"`
}

// Report is `agent accounts detect --json`.
type Report struct {
	CLIs []CLI   `json:"clis"`
	At   float64 `json:"at"`
}

// System is what detection looks at, so tests can stand in for it.
type System struct {
	LookPath func(string) (string, error)
	// Run runs a program with no input and returns its output and exit status (-1 when it couldn't run).
	Run    func(path string, args ...string) (string, int)
	Getenv func(string) string
	Exists func(string) bool
	Home   string
	GOOS   string
}

// Local is this computer.
func Local() System {
	home, _ := os.UserHomeDir()
	return System{
		LookPath: process.LookPath,
		Run:      run,
		Getenv:   os.Getenv,
		Exists: func(path string) bool {
			info, err := os.Stat(path)
			return err == nil && !info.IsDir()
		},
		Home: home,
		GOOS: runtime.GOOS,
	}
}

// run gives a CLI 10 seconds, no standard input, and keeps only what it prints.
func run(path string, args ...string) (string, int) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, path, args...)
	command.Stdin = nil
	output, err := command.CombinedOutput()
	var exit *exec.ExitError
	switch {
	case err == nil:
		return string(output), 0
	case errors.As(err, &exit) && ctx.Err() == nil:
		return string(output), exit.ExitCode()
	default:
		return string(output), -1
	}
}

// Detect looks for every provider's CLI.
func Detect(system System) Report {
	report := Report{At: float64(time.Now().UnixMilli()) / 1000}
	for _, provider := range Providers {
		report.CLIs = append(report.CLIs, detect(system, provider))
	}
	return report
}

func detect(system System, provider string) CLI {
	cli := CLI{Provider: provider, Auth: "unknown", Modes: []string{"runner"}}
	if provider == "copilot" {
		cli.Modes = []string{"runner", "token"}
	}
	path, err := system.LookPath(provider)
	if err != nil {
		cli.Note = provider + " isn't installed here"
		return cli
	}
	cli.Installed = true
	if output, status := system.Run(path, "--version"); status == 0 {
		cli.Version = firstLine(output, 60)
	}
	no, yes := false, true
	switch provider {
	case "claude":
		// Claude Code keeps its sign-in in .credentials.json (Linux, Windows) or the macOS Keychain.
		// Only whether the file is there is checked; it's never opened.
		if system.Getenv("ANTHROPIC_API_KEY") != "" || system.Getenv("ANTHROPIC_AUTH_TOKEN") != "" {
			cli.Auth, cli.Method, cli.Subscription = "signed_in", "api_key", &no
			cli.Note = "ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is set, so Claude Code uses it rather than a Claude subscription"
			break
		}
		folder := system.Getenv("CLAUDE_CONFIG_DIR")
		if folder == "" {
			folder = filepath.Join(system.Home, ".claude")
		}
		switch {
		case system.Exists(filepath.Join(folder, ".credentials.json")):
			cli.Auth, cli.Method, cli.Subscription = "signed_in", "claude.ai", &yes
		case system.GOOS == "darwin":
			cli.Note = "Claude Code keeps its sign-in in the macOS Keychain, which isn't read: run claude to check"
		default:
			cli.Auth, cli.Reauthenticate = "signed_out", true
			cli.Note = "Run claude and sign in with your Claude account"
		}
	case "codex":
		// Codex's documented status command; its output may name a masked API key, so only its kind is kept.
		output, status := system.Run(path, "login", "status")
		switch {
		case status == 0 && strings.Contains(strings.ToLower(output), "chatgpt"):
			cli.Auth, cli.Method, cli.Subscription = "signed_in", "chatgpt", &yes
		case status == 0 && strings.Contains(strings.ToLower(output), "api key"):
			cli.Auth, cli.Method, cli.Subscription = "signed_in", "api_key", &no
			cli.Note = "Codex is signed in with an API key, not a ChatGPT plan: run codex login"
		case status == 0:
			cli.Auth = "signed_in"
		case status > 0:
			cli.Auth, cli.Reauthenticate = "signed_out", true
			cli.Note = "Run codex login (codex login --device-auth without a browser)"
		}
	case "copilot":
		// Copilot CLI has no status command; a token in its documented variables is a sign-in.
		for _, name := range []string{"COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"} {
			if system.Getenv(name) != "" {
				cli.Auth, cli.Method = "signed_in", "token:"+name
				break
			}
		}
		if cli.Auth == "unknown" {
			cli.Note = "Copilot CLI can't say whether it's signed in: run copilot and /login if it asks"
		}
	}
	return cli
}

func firstLine(text string, size int) string {
	line, _, _ := strings.Cut(strings.TrimSpace(text), "\n")
	line = strings.TrimSpace(line)
	if len(line) > size {
		line = line[:size]
	}
	return line
}
