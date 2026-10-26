package containers

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"

	"ai-coding-agent-proxy/machine/internal/process"
)

// CommandEnv names a JSON list of arguments run as the CLI instead of docker or podman
// (tests use a fake one).
const CommandEnv = "AI_PROXY_CONTAINER_COMMAND"

// CLI runs the container CLI with this machine's environment, so DOCKER_HOST and
// docker contexts work as they do in a terminal here.
type CLI struct {
	Name string
	// Program runs instead of Name from PATH (tests).
	Program []string
}

// NewCLI is the CLI the machine's settings name: docker unless they say podman.
func NewCLI(name string) CLI {
	if name != "podman" {
		name = "docker"
	}
	return CLI{Name: name}
}

// Command is the program and leading arguments to run, or nil when it isn't installed.
func (c CLI) Command() []string {
	if c.Program != nil {
		return c.Program
	}
	if value := os.Getenv(CommandEnv); value != "" {
		var command []string
		if json.Unmarshal([]byte(value), &command) == nil && len(command) > 0 {
			return command
		}
		return nil
	}
	found, err := exec.LookPath(c.Name)
	if err != nil {
		return nil
	}
	return []string{found}
}

func (c CLI) missing() string {
	return c.Name + " isn't installed or isn't on PATH"
}

func (c CLI) command(ctx context.Context, args []string) (*exec.Cmd, error) {
	command := c.Command()
	if command == nil {
		return nil, errors.New(c.missing())
	}
	cmd := exec.CommandContext(ctx, command[0], append(command[1:], args...)...)
	cmd.Env = os.Environ()
	process.Quiet(cmd)
	return cmd, nil
}

// Run runs one command and returns its exit code and output. 127 means the CLI is
// missing, 124 that it took longer than timeout.
func (c CLI) Run(timeout time.Duration, args ...string) (int, string, string) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd, err := c.command(ctx, args)
	if err != nil {
		return 127, "", err.Error()
	}
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	err = cmd.Run()
	if ctx.Err() == context.DeadlineExceeded {
		return 124, stdout.String(), fmt.Sprintf("%s %s took longer than %d s", c.Name, args[0], int(timeout.Seconds()))
	}
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		return exit.ExitCode(), stdout.String(), stderr.String()
	}
	if err != nil {
		return 127, stdout.String(), err.Error()
	}
	return 0, stdout.String(), stderr.String()
}

// Combined runs one command with stdout and stderr together, as `docker logs` shows both.
func (c CLI) Combined(timeout time.Duration, args ...string) string {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd, err := c.command(ctx, args)
	if err != nil {
		return ""
	}
	output, err := cmd.CombinedOutput()
	if err != nil && len(output) == 0 {
		return err.Error()
	}
	return string(output)
}

// Stream runs a long command (an image pull), passing each line on as it arrives.
func (c CLI) Stream(timeout time.Duration, onLine func(string), args ...string) int {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd, err := c.command(ctx, args)
	if err != nil {
		onLine(err.Error())
		return 127
	}
	reader, writer, err := os.Pipe()
	if err != nil {
		onLine(err.Error())
		return 127
	}
	cmd.Stdout, cmd.Stderr = writer, writer
	if err := cmd.Start(); err != nil {
		reader.Close()
		writer.Close()
		onLine(err.Error())
		return 127
	}
	writer.Close()
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 64*1024), 1024*1024)
	for scanner.Scan() {
		if line := strings.TrimRight(scanner.Text(), "\r\n "); strings.TrimSpace(line) != "" {
			onLine(line)
		}
	}
	reader.Close()
	err = cmd.Wait()
	if ctx.Err() == context.DeadlineExceeded {
		onLine("Timed out")
		return 124
	}
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		return exit.ExitCode()
	}
	if err != nil {
		return 1
	}
	return 0
}

// Runtime is whether the CLI is installed and its engine answers.
type Runtime struct {
	CLI     string `json:"cli"`
	Found   bool   `json:"found"`
	Online  bool   `json:"online"`
	Version string `json:"version"`
	Error   string `json:"error"`
}

// Probe asks the CLI for its version, which needs the engine up.
func (c CLI) Probe() Runtime {
	runtime := Runtime{CLI: c.Name}
	if c.Command() == nil {
		runtime.Error = c.missing()
		return runtime
	}
	runtime.Found = true
	code, out, errText := c.Run(15*time.Second, "version", "--format", "{{json .}}")
	var version struct {
		Client struct{ Version string }
		Server *struct{ Version string }
	}
	// The first line: podman and the fake print one JSON object, a failing docker adds its error after.
	line, _, _ := strings.Cut(strings.TrimSpace(out), "\n")
	_ = json.Unmarshal([]byte(line), &version)
	// Docker always answers with its engine's part (Server). Podman on Linux runs without a
	// service and has none; where it needs one (a podman machine), it fails without it.
	runtime.Online = code == 0 && (version.Server != nil || c.Name == "podman")
	runtime.Version = version.Client.Version
	if version.Server != nil && version.Server.Version != "" {
		runtime.Version = version.Server.Version
	}
	if !runtime.Online {
		message := strings.TrimSpace(errText)
		if message == "" {
			message = strings.TrimSpace(out)
		}
		if message == "" {
			message = c.Name + " version failed"
		}
		if len(message) > 500 {
			message = message[len(message)-500:]
		}
		runtime.Error = message
	}
	return runtime
}

// State is a container as `inspect` describes it.
type State struct {
	Status     string `json:"status"`
	Running    bool   `json:"running"`
	Health     any    `json:"health"`
	StartedAt  any    `json:"started_at"`
	FinishedAt any    `json:"finished_at"`
	ExitCode   any    `json:"exit_code"`
	Error      any    `json:"error"`
	Spec       any    `json:"spec"`
	Image      any    `json:"image"`
}

// Inspect is the state of the named containers (server names, without the prefix); missing ones are left out.
func (c CLI) Inspect(names []string) map[string]State {
	found := map[string]State{}
	if len(names) == 0 || c.Command() == nil {
		return found
	}
	args := []string{"inspect"}
	for _, name := range names {
		args = append(args, Prefix+name)
	}
	// inspect prints the containers it found even when some are missing (and exits 1).
	_, out, _ := c.Run(20*time.Second, args...)
	var entries []struct {
		Name  string
		State struct {
			Status      string
			Running     bool
			Health      *struct{ Status string }
			Healthcheck *struct{ Status string }
			StartedAt   string
			FinishedAt  string
			ExitCode    int
			Error       string
		}
		Config struct {
			Labels map[string]string
			Image  string
		}
	}
	if json.Unmarshal([]byte(strings.TrimSpace(out)), &entries) != nil {
		return found
	}
	for _, entry := range entries {
		name := entry.Config.Labels[label]
		if name == "" {
			name = strings.TrimPrefix(strings.TrimPrefix(entry.Name, "/"), Prefix)
		}
		status := strings.ToLower(entry.State.Status)
		if status == "" {
			status = "unknown"
		}
		state := State{
			Status: status, Running: entry.State.Running, StartedAt: entry.State.StartedAt, FinishedAt: entry.State.FinishedAt,
			ExitCode: entry.State.ExitCode, Error: nil, Spec: nil, Image: entry.Config.Image,
		}
		health := entry.State.Health
		if health == nil {
			health = entry.State.Healthcheck
		}
		if health != nil && health.Status != "" {
			state.Health = health.Status
		}
		if entry.State.Error != "" {
			state.Error = entry.State.Error
		}
		if spec, ok := entry.Config.Labels["ai-proxy.spec"]; ok {
			state.Spec = spec
		}
		found[name] = state
	}
	return found
}
