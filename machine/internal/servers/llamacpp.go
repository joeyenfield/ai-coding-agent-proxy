package servers

// llama.cpp's llama-server, run in router mode: started without a model, it serves every
// model in its cache (downloaded from Hugging Face through its own /models API) and folder,
// loading each on the first request for it and unloading the least recently used. So one
// process on one port is the machine's llama.cpp provider, managed like its Ollama: started
// and stopped here (only the one agent-machine started), with the LLAMA_ARG_* settings saved
// from the console (llamacpp_settings.go), and installed from the console
// (llamacpp_install.go). It always listens on loopback: agents reach it through the
// machine's tunnel, so it never needs an API key.

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"ai-control-mesh/machine/internal/process"
	"ai-control-mesh/machine/internal/providers"
	"ai-control-mesh/machine/internal/settings"
)

const (
	// LlamaEnv is a command to run instead of llama-server, as a JSON list (tests use a fake).
	LlamaEnv = "AI_PROXY_LLAMACPP_COMMAND"
	// LlamaPort is where llama-server listens by default, and where the fleet looks for it.
	LlamaPort = providers.LlamaPort
)

func (m *Manager) llamaPort() int {
	if m.LlamaPort > 0 {
		return m.LlamaPort
	}
	return providers.LlamaServerPort()
}

func llamaLog() string { return filepath.Join(settings.Home(), "llamacpp.log") }

func serverName() string {
	if runtime.GOOS == "windows" {
		return "llama-server.exe"
	}
	return "llama-server"
}

// termuxPrefix is Termux's prefix when agent-machine runs in Termux on Android, else "".
func termuxPrefix() string {
	prefix := os.Getenv("PREFIX")
	if strings.Contains(prefix, "com.termux") {
		return prefix
	}
	if os.Getenv("TERMUX_VERSION") != "" {
		if prefix == "" {
			prefix = "/data/data/com.termux/files/usr"
		}
		return prefix
	}
	return ""
}

// llamaCommand is the llama-server to run: the one installed from the console, else one on
// PATH or where Termux and Homebrew put it.
func llamaCommand() []string {
	return command(LlamaEnv, func() string {
		if record := loadInstall(); record.Binary != "" {
			if info, err := os.Stat(record.Binary); err == nil && !info.IsDir() {
				return record.Binary
			}
		}
		if found, err := exec.LookPath(serverName()); err == nil {
			return found
		}
		candidates := []string{"/opt/homebrew/bin/llama-server", "/usr/local/bin/llama-server", "/usr/bin/llama-server"}
		if prefix := termuxPrefix(); prefix != "" {
			candidates = append([]string{filepath.Join(prefix, "bin", "llama-server")}, candidates...)
		}
		for _, candidate := range candidates {
			if info, err := os.Stat(candidate); err == nil && !info.IsDir() {
				return candidate
			}
		}
		return ""
	})
}

// llamaRunning tells whether a llama-server answers on the port: /health is 200 when it's
// ready and 503 while it loads, and it never needs a key.
func (m *Manager) llamaRunning() bool {
	response, err := settings.Client(1500 * time.Millisecond).Get("http://127.0.0.1:" + strconv.Itoa(m.llamaPort()) + "/health")
	if err != nil {
		return false
	}
	response.Body.Close()
	return response.StatusCode == http.StatusOK || response.StatusCode == http.StatusServiceUnavailable
}

var (
	// "version: 0.8.0-dev (build 12032, commit dde879bc4)" now, "version: 12032 (dde879bc4)" before.
	versionLine = regexp.MustCompile(`version:\s*(?:\S+\s*\(build (\d+)|(\d+)\s*\([0-9a-f]+\))`)
	// llama-server --version is asked once per binary, not every minute.
	versions   = map[string]string{}
	versionsMu sync.Mutex
)

// llamaVersion is the build of a llama-server ("b12032"), from its --version.
func llamaVersion(program []string) string {
	if len(program) == 0 {
		return ""
	}
	key := strings.Join(program, "\x00")
	if info, err := os.Stat(program[0]); err == nil {
		key += "\x00" + info.ModTime().String()
	}
	versionsMu.Lock()
	defer versionsMu.Unlock()
	if version, ok := versions[key]; ok {
		return version
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, program[0], append(program[1:], "--version")...)
	cmd.Env = withLibraries(os.Environ(), program[0])
	process.Quiet(cmd)
	output, _ := cmd.CombinedOutput()
	version := parseVersion(output)
	if prefix := termuxPrefix(); version == "" && prefix != "" && strings.HasPrefix(program[0], prefix) {
		// Termux builds from a source tarball, whose --version names no build: its package does.
		version = termuxVersion(prefix)
	}
	versions[key] = version
	return version
}

// parseVersion is the build in llama-server's --version, as its releases name it ("b12032").
func parseVersion(output []byte) string {
	found := versionLine.FindSubmatch(output)
	if found == nil {
		return ""
	}
	if len(found[1]) > 0 {
		return "b" + string(found[1])
	}
	return "b" + string(found[2])
}

// termuxVersion is Termux's llama-cpp package version, as llama.cpp names it: "b9590" for the
// old "0.0.0-b9590" packages, else the release ("0.8.0").
func termuxVersion(prefix string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, filepath.Join(prefix, "bin", "dpkg-query"), "-W", "-f=${Version}", "llama-cpp")
	process.Quiet(cmd)
	output, err := cmd.Output()
	if err != nil {
		return ""
	}
	return packageVersion(string(output))
}

func packageVersion(text string) string {
	text = strings.TrimSpace(text)
	if _, after, found := strings.Cut(text, ":"); found {
		text = after // an epoch
	}
	if _, build, found := strings.Cut(text, "-b"); found && build != "" {
		build, _, _ = strings.Cut(build, "-")
		return "b" + build
	}
	text, _, _ = strings.Cut(text, "-")
	return text
}

// llamaPID is the llama-server this agent-machine started, while it still runs.
func llamaPID() int {
	value := loadState()
	if value.LlamaPID > 0 && process.Alive(value.LlamaPID) {
		return value.LlamaPID
	}
	return 0
}

func (m *Manager) llamaState() map[string]any {
	program := llamaCommand()
	record := loadInstall()
	result := map[string]any{
		"found": program != nil, "running": m.llamaRunning(), "managed": llamaPID() > 0, "port": m.llamaPort(),
		"version": llamaVersion(program), "install": installProgress(),
		// Where it came from: the console's install (and which build), or found here.
		"source": nil, "variant": nil, "release": nil,
	}
	if program != nil && record.Binary != "" && program[0] == record.Binary {
		result["source"], result["variant"], result["release"] = record.Source, record.Variant, record.Release
	} else if program != nil {
		result["source"] = "found"
	}
	if termuxPrefix() != "" {
		result["platform"] = "termux"
	} else {
		result["platform"] = runtime.GOOS + "-" + runtime.GOARCH
	}
	return result
}

// withLibraries lets a llama-server unpacked from a release find the libraries beside it
// (its backends, and CUDA's runtime): Linux and macOS don't look there by themselves.
func withLibraries(env []string, binary string) []string {
	name := map[string]string{"linux": "LD_LIBRARY_PATH", "android": "LD_LIBRARY_PATH", "darwin": "DYLD_LIBRARY_PATH", "freebsd": "LD_LIBRARY_PATH"}[runtime.GOOS]
	if name == "" || !strings.HasPrefix(filepath.Clean(binary), filepath.Clean(installRoot())+string(filepath.Separator)) {
		return env
	}
	dir := filepath.Dir(binary)
	for i, entry := range env {
		if strings.HasPrefix(entry, name+"=") {
			env[i] = name + "=" + dir + string(os.PathListSeparator) + strings.TrimPrefix(entry, name+"=")
			return env
		}
	}
	return append(env, name+"="+dir)
}

func (m *Manager) startLlama() error {
	if m.llamaRunning() {
		return nil
	}
	program := llamaCommand()
	if program == nil {
		return errors.New("llama.cpp isn't installed here: install it from the console, or put llama-server on PATH")
	}
	if installing() {
		return errors.New("llama.cpp is being installed: start it when that's done")
	}
	if err := os.MkdirAll(settings.Home(), 0o700); err != nil {
		return err
	}
	logFile, err := os.OpenFile(llamaLog(), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	defer logFile.Close()
	port := strconv.Itoa(m.llamaPort())
	fmt.Fprintf(logFile, "# agent-machine: starting %s (router mode) on 127.0.0.1:%s at %s\n", strings.Join(program, " "), port, time.Now().Format(time.RFC3339))
	config := LoadLlamaConfig()
	// The router refuses to start without its GGUF folder.
	if folder := LlamaManagedEnv(config)["LLAMA_ARG_MODELS_DIR"]; folder != nil {
		if err := os.MkdirAll(*folder, 0o755); err != nil {
			return fmt.Errorf("couldn't make the GGUF folder %s: %v", *folder, err)
		}
	}
	env := publicEnv(LlamaManagedEnv(config))
	for _, name := range sortedKeys(env) {
		fmt.Fprintf(logFile, "#   %s=%v\n", name, orUnset(env[name]))
	}
	// No model: router mode. Where it listens is agent-machine's to say, on the command line,
	// which wins over any variable.
	cmd := exec.Command(program[0], append(program[1:], "--host", "127.0.0.1", "--port", port)...)
	cmd.Env = withLibraries(LlamaEnvironment(os.Environ(), config), program[0])
	cmd.Dir = settings.Home()
	cmd.Stdin, cmd.Stdout, cmd.Stderr = nil, logFile, logFile
	process.Detach(cmd)
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("couldn't start llama-server: %v", err)
	}
	pid := cmd.Process.Pid
	exited := make(chan struct{})
	go func() {
		_ = cmd.Wait()
		close(exited)
	}()
	saved := loadState()
	saved.LlamaPID, saved.LlamaEnv = pid, env
	if err := saved.save(); err != nil {
		return err
	}
	deadline := time.Now().Add(60 * time.Second)
	for time.Now().Before(deadline) {
		if m.llamaRunning() {
			return nil
		}
		select {
		case <-exited:
			return fmt.Errorf("llama-server exited while starting: %s", tail(llamaLog(), 600))
		case <-time.After(250 * time.Millisecond):
		}
	}
	return errors.New("llama-server started but doesn't answer yet; see " + llamaLog())
}

func (m *Manager) stopLlama() error {
	pid := llamaPID()
	if pid == 0 {
		if m.llamaRunning() {
			return errors.New("a llama-server agent-machine didn't start is running on this port, so it can't stop it; stop it on this computer, then start llama.cpp from the console")
		}
		return nil
	}
	// The router's own instances are in its process group, and go with it.
	if err := process.StopTree(pid); err != nil {
		return err
	}
	for deadline := time.Now().Add(20 * time.Second); time.Now().Before(deadline); time.Sleep(200 * time.Millisecond) {
		if !process.Alive(pid) && !m.llamaRunning() {
			break
		}
	}
	if process.Alive(pid) {
		// Keep the PID: clearing it would make a later stop say llama-server was
		// started outside agent-machine, for a process this machine itself started.
		return errors.New("llama-server is still running after being asked to stop; try again")
	}
	saved := loadState()
	saved.LlamaPID, saved.LlamaEnv = 0, nil
	_ = saved.save()
	if m.llamaRunning() {
		return errors.New("llama-server is still answering after being stopped")
	}
	return nil
}

func (m *Manager) llamaAction(request Request) error {
	switch request.Op {
	case "settings", "install_options":
		return nil
	case "configure":
		_, err := SaveLlamaConfig(request.Config)
		return err
	case "start":
		return m.startLlama()
	case "stop":
		return m.stopLlama()
	case "restart":
		if err := m.stopLlama(); err != nil {
			return err
		}
		return m.startLlama()
	case "install":
		// A release goes into a folder of its own, so a running llama-server keeps serving
		// until it's restarted onto the new one.
		return startInstall(request.Variant)
	case "uninstall":
		if llamaPID() > 0 {
			return errors.New("stop llama.cpp before removing it")
		}
		return uninstall()
	}
	return fmt.Errorf("unknown request: %s llamacpp", request.Op)
}

func sortedKeys(values map[string]any) []string {
	names := make([]string, 0, len(values))
	for name := range values {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}
