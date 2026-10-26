// Package servers starts and stops the model servers installed on this machine when the
// server asks: Ollama (`ollama serve`, with the OLLAMA_* settings saved from the console)
// and Strata (a saved profile, through Strata's own `strata runtime start|stop`). It's off
// until `agent-machine servers on`.
//
// It only stops what it can own: the Ollama it started itself (an Ollama run by its tray
// app or a service is left to that), and Strata instances, which Strata's runtime owns
// and stops cleanly. Requests only name a kind and, for Strata, one of the machine's own
// saved profiles, so the server can't run anything else here.
package servers

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"

	"ai-coding-agent-proxy/machine/internal/process"
	"ai-coding-agent-proxy/machine/internal/settings"
)

const (
	// Commands to run instead of ollama or strata, as JSON lists of arguments (tests use fakes).
	OllamaEnv = "AI_PROXY_OLLAMA_COMMAND"
	StrataEnv = "AI_PROXY_STRATA_COMMAND"
	// OllamaPort is where Ollama listens by default, and where the fleet looks for it.
	OllamaPort = 11434
)

var profileName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)

// Request is the server's {"type": "server", ...}: start, stop, restart or status of one
// kind, or Ollama's settings (settings, configure).
type Request struct {
	Type    string `json:"type"`
	ID      string `json:"request"`
	Op      string `json:"op"`
	Kind    string `json:"kind"`
	Profile string `json:"profile"`
	// Bind is where Ollama listens: 127.0.0.1 when the server is this computer, else 0.0.0.0.
	Bind string `json:"bind"`
	// Config is Ollama's settings, for configure.
	Config map[string]any `json:"config"`
}

// Manager answers the server's requests and remembers the Ollama it started.
type Manager struct {
	// Machine reads the machine's settings fresh for each request; tests replace it.
	Machine func() *settings.Machine
	// Port is Ollama's port; tests use another so a real Ollama is never touched.
	Port int
	mu   sync.Mutex
}

func (m *Manager) machine() *settings.Machine {
	if m.Machine != nil {
		return m.Machine()
	}
	return settings.LoadMachine()
}

func (m *Manager) port() int {
	if m.Port > 0 {
		return m.Port
	}
	return OllamaPort
}

// Describe is the "servers" part of the machine's hello: what's installed and running.
func (m *Manager) Describe() map[string]any {
	if !m.machine().Servers {
		return map[string]any{"enabled": false}
	}
	return map[string]any{"enabled": true, "ollama": m.ollamaState(), "strata": strataState()}
}

// Handle answers one request, always with a server_result for its id.
func (m *Manager) Handle(request Request) map[string]any {
	m.mu.Lock()
	defer m.mu.Unlock()
	answer := map[string]any{"type": "server_result", "request": request.ID}
	if err := m.handle(request); err != nil {
		answer["ok"], answer["error"] = false, err.Error()
	} else {
		answer["ok"] = true
	}
	if request.Kind == "ollama" && (request.Op == "settings" || request.Op == "configure") && m.machine().Servers {
		answer["ollama_settings"] = m.ollamaSettings()
	}
	answer["servers"] = m.Describe()
	return answer
}

func (m *Manager) handle(request Request) error {
	if !m.machine().Servers {
		return errors.New("Starting model servers is off on this machine (agent-machine servers on)")
	}
	switch {
	case request.Op == "status":
		return nil
	case request.Kind == "ollama" && request.Op == "settings":
		return nil
	case request.Kind == "ollama" && request.Op == "configure":
		_, err := SaveConfig(request.Config)
		return err
	case request.Kind == "ollama" && (request.Op == "start" || request.Op == "restart"):
		bind := request.Bind
		if bind == "" {
			bind = "127.0.0.1"
		}
		if bind != "127.0.0.1" && bind != "0.0.0.0" {
			return fmt.Errorf("Ollama listens on 127.0.0.1 or 0.0.0.0, not %q", bind)
		}
		if ollamaService() {
			return m.serviceAction(request.Op, bind)
		}
		if request.Op == "restart" {
			if err := m.stopOllama(); err != nil {
				return err
			}
		}
		return m.startOllama(bind)
	case request.Kind == "ollama" && request.Op == "stop":
		if ollamaService() {
			return m.serviceAction("stop", "")
		}
		return m.stopOllama()
	case request.Kind == "strata" && (request.Op == "start" || request.Op == "stop"):
		return strataAction(request.Op, request.Profile)
	}
	return fmt.Errorf("unknown request: %s %s", request.Op, request.Kind)
}

// command is the program to run: the override from the environment, else what find returns.
func command(env string, find func() string) []string {
	if value := os.Getenv(env); value != "" {
		var parts []string
		if json.Unmarshal([]byte(value), &parts) == nil && len(parts) > 0 {
			return parts
		}
		return nil
	}
	if found := find(); found != "" {
		return []string{found}
	}
	return nil
}

// Ollama

// ollamaCommand is ollama on PATH, or where its installer puts it.
func ollamaCommand() []string {
	return command(OllamaEnv, func() string {
		if found, err := exec.LookPath("ollama"); err == nil {
			return found
		}
		var candidates []string
		switch runtime.GOOS {
		case "windows":
			candidates = []string{filepath.Join(os.Getenv("LOCALAPPDATA"), "Programs", "Ollama", "ollama.exe")}
		case "darwin":
			candidates = []string{"/Applications/Ollama.app/Contents/Resources/ollama", "/usr/local/bin/ollama", "/opt/homebrew/bin/ollama"}
		default:
			candidates = []string{"/usr/local/bin/ollama", "/usr/bin/ollama"}
		}
		for _, candidate := range candidates {
			if info, err := os.Stat(candidate); err == nil && !info.IsDir() {
				return candidate
			}
		}
		return ""
	})
}

// ollamaVersion is the version of the Ollama answering on the port, or "" when none does.
func (m *Manager) ollamaVersion() string {
	response, err := settings.Client(1500 * time.Millisecond).Get("http://127.0.0.1:" + strconv.Itoa(m.port()) + "/api/version")
	if err != nil {
		return ""
	}
	defer response.Body.Close()
	var body struct {
		Version string `json:"version"`
	}
	if response.StatusCode != http.StatusOK || json.NewDecoder(response.Body).Decode(&body) != nil {
		return ""
	}
	if body.Version == "" {
		return "unknown"
	}
	return body.Version
}

// state is servers.json in agent-machine's folder: the Ollama it started.
type state struct {
	OllamaPID  int    `json:"ollama_pid,omitempty"`
	OllamaBind string `json:"ollama_bind,omitempty"`
	// OllamaEnv is the managed environment it was started with, to tell whether it runs the saved settings.
	OllamaEnv map[string]any `json:"ollama_env,omitempty"`
	// Service: it was started through its systemd service (systemd.go), not by agent-machine itself.
	Service bool `json:"service,omitempty"`
}

func statePath() string { return filepath.Join(settings.Home(), "servers.json") }

func loadState() state {
	var value state
	if data, err := os.ReadFile(statePath()); err == nil {
		_ = json.Unmarshal(data, &value)
	}
	return value
}

func (s state) save() error {
	data, _ := json.MarshalIndent(s, "", "  ")
	if err := os.MkdirAll(settings.Home(), 0o700); err != nil {
		return err
	}
	return os.WriteFile(statePath(), append(data, '\n'), 0o600)
}

// managedPID is the Ollama this agent-machine started, while it still runs.
func managedPID() int {
	value := loadState()
	if value.OllamaPID > 0 && process.Alive(value.OllamaPID) {
		return value.OllamaPID
	}
	return 0
}

func (m *Manager) ollamaState() map[string]any {
	version := m.ollamaVersion()
	pid := managedPID()
	result := map[string]any{"found": ollamaCommand() != nil, "running": version != "", "version": version, "managed": pid > 0, "port": m.port()}
	if pid > 0 {
		result["bind"] = loadState().OllamaBind
	}
	// A systemd service is agent-machine's to start and stop once it's allowed to (systemd.go).
	if ollamaService() {
		allowed := serviceAllowed()
		result["service"], result["service_allowed"], result["managed"] = "systemd", allowed, allowed
		if allowed {
			result["bind"] = loadState().OllamaBind
		}
	}
	return result
}

func ollamaLog() string { return filepath.Join(settings.Home(), "ollama.log") }

func (m *Manager) startOllama(bind string) error {
	if m.ollamaVersion() != "" {
		return nil
	}
	program := ollamaCommand()
	if program == nil {
		return errors.New("Ollama isn't installed here (https://ollama.com/download), or isn't on PATH")
	}
	if err := os.MkdirAll(settings.Home(), 0o700); err != nil {
		return err
	}
	logFile, err := os.OpenFile(ollamaLog(), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	defer logFile.Close()
	fmt.Fprintf(logFile, "# agent-machine: starting %s serve on %s:%d at %s\n", strings.Join(program, " "), bind, m.port(), time.Now().Format(time.RFC3339))
	config := LoadConfig()
	for name, value := range publicEnv(ManagedEnv(config)) {
		fmt.Fprintf(logFile, "#   %s=%v\n", name, orUnset(value))
	}
	cmd := exec.Command(program[0], append(program[1:], "serve")...)
	// The saved settings, then OLLAMA_HOST, which is where it listens: loopback for the
	// server on this computer, else every address, so the server can reach it.
	cmd.Env = append(Environment(os.Environ(), config), fmt.Sprintf("OLLAMA_HOST=%s:%d", bind, m.port()))
	cmd.Stdin, cmd.Stdout, cmd.Stderr = nil, logFile, logFile
	process.Detach(cmd)
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("couldn't start Ollama: %v", err)
	}
	pid := cmd.Process.Pid
	exited := make(chan struct{})
	go func() {
		_ = cmd.Wait()
		close(exited)
	}()
	if err := (state{OllamaPID: pid, OllamaBind: bind, OllamaEnv: publicEnv(ManagedEnv(config))}).save(); err != nil {
		return err
	}
	deadline := time.Now().Add(30 * time.Second)
	for time.Now().Before(deadline) {
		if m.ollamaVersion() != "" {
			return nil
		}
		select {
		case <-exited:
			return fmt.Errorf("Ollama exited while starting: %s", tail(ollamaLog(), 600))
		case <-time.After(250 * time.Millisecond):
		}
	}
	return errors.New("Ollama started but doesn't answer yet; see " + ollamaLog())
}

func (m *Manager) stopOllama() error {
	pid := managedPID()
	if pid == 0 {
		if m.ollamaVersion() != "" {
			return errors.New("Ollama runs here, but agent-machine didn't start it; stop it where it was started (its tray app)")
		}
		return nil
	}
	if err := process.StopTree(pid); err != nil {
		return err
	}
	for deadline := time.Now().Add(15 * time.Second); time.Now().Before(deadline); time.Sleep(200 * time.Millisecond) {
		if !process.Alive(pid) && m.ollamaVersion() == "" {
			break
		}
	}
	_ = (state{}).save()
	if m.ollamaVersion() != "" {
		return errors.New("Ollama is still answering after being stopped")
	}
	return nil
}

func orUnset(value any) any {
	if value == nil {
		return "(unset)"
	}
	return value
}

func tail(path string, limit int) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return "no log"
	}
	text := strings.TrimSpace(string(data))
	if len(text) > limit {
		text = "…" + text[len(text)-limit:]
	}
	return text
}

// Strata

// strataCommand is strata on PATH, or the terminal Strata's desktop app installs.
func strataCommand() []string {
	return command(StrataEnv, func() string {
		if found, err := exec.LookPath("strata"); err == nil {
			return found
		}
		if runtime.GOOS == "windows" {
			candidate := filepath.Join(os.Getenv("LOCALAPPDATA"), "Programs", "Strata", "control", "bin", "strata.cmd")
			if _, err := os.Stat(candidate); err == nil {
				return candidate
			}
		}
		return ""
	})
}

// strata runs a strata command with --json and decodes its answer.
func strata(timeout time.Duration, args ...string) (map[string]any, error) {
	program := strataCommand()
	if program == nil {
		return nil, errors.New("Strata isn't installed here, or its strata command isn't on PATH")
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, program[0], append(append(program[1:], args...), "--json")...)
	process.Quiet(cmd)
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	err := cmd.Run()
	var answer map[string]any
	if json.Unmarshal(lastLine(stdout.Bytes()), &answer) != nil {
		message := strings.TrimSpace(stderr.String() + " " + stdout.String())
		if err != nil {
			return nil, fmt.Errorf("strata %s: %v %s", args[0], err, message)
		}
		return nil, fmt.Errorf("strata %s: unexpected answer %q", args[0], message)
	}
	if ok, _ := answer["ok"].(bool); !ok {
		return answer, fmt.Errorf("strata %s: %s", strings.Join(args, " "), strataError(answer, stderr.String()))
	}
	return answer, nil
}

// lastLine is the last non-empty line: Strata prints its JSON answer last.
func lastLine(output []byte) []byte {
	lines := bytes.Split(bytes.TrimSpace(output), []byte("\n"))
	return bytes.TrimSpace(lines[len(lines)-1])
}

func strataError(answer map[string]any, stderr string) string {
	for _, key := range []string{"error", "message", "detail", "reason"} {
		switch value := answer[key].(type) {
		case string:
			if value != "" {
				return value
			}
		case map[string]any:
			if text, ok := value["message"].(string); ok {
				return text
			}
		}
	}
	if text := strings.TrimSpace(stderr); text != "" {
		return text
	}
	return "it refused"
}

// profiles is the ids of the machine's saved Strata profiles.
func profiles() ([]string, error) {
	answer, err := strata(30*time.Second, "profile", "list")
	if err != nil {
		return nil, err
	}
	var ids []string
	items, _ := answer["profiles"].([]any)
	for _, item := range items {
		if profile, ok := item.(map[string]any); ok {
			if id, ok := profile["id"].(string); ok && profileName.MatchString(id) {
				ids = append(ids, id)
			}
		}
	}
	return ids, nil
}

func strataState() map[string]any {
	if strataCommand() == nil {
		return map[string]any{"found": false, "profiles": []any{}}
	}
	ids, err := profiles()
	if err != nil {
		return map[string]any{"found": true, "profiles": []any{}, "error": err.Error()}
	}
	list := []any{}
	for _, id := range ids {
		entry := map[string]any{"id": id, "state": "unknown"}
		if answer, err := strata(30*time.Second, "runtime", "status", "--profile", id); err == nil {
			if text, ok := answer["state"].(string); ok {
				entry["state"] = text
			}
			for _, key := range []string{"api_url", "url"} {
				if text, ok := answer[key].(string); ok && text != "" {
					entry["url"] = text
					break
				}
			}
		} else {
			entry["error"] = err.Error()
		}
		list = append(list, entry)
	}
	return map[string]any{"found": true, "profiles": list}
}

func strataAction(op, profile string) error {
	if !profileName.MatchString(profile) {
		return errors.New("name one of this machine's Strata profiles")
	}
	ids, err := profiles()
	if err != nil {
		return err
	}
	known := false
	for _, id := range ids {
		known = known || id == profile
	}
	if !known {
		return fmt.Errorf("this machine has no Strata profile %q", profile)
	}
	if op == "start" {
		_, err = strata(3*time.Minute, "runtime", "start", "--profile", profile)
	} else {
		// Strata drains requests in flight and unloads the model before it stops.
		_, err = strata(5*time.Minute, "runtime", "stop", "--profile", profile)
	}
	return err
}
