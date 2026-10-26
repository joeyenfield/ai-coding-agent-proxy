package servers

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"time"

	"ai-coding-agent-proxy/machine/internal/process"
)

// An Ollama installed as a systemd service (Ollama's own Linux installer sets up
// ollama.service, run as the ollama user) belongs to root: agent-machine, running as you,
// can't restart it or change its settings. `sudo agent-machine servers service` lets it,
// once and narrowly:
//
//   - a sudoers rule lets this user run only `systemctl start|stop|restart ollama`;
//   - a drop-in makes the service read its environment from ServiceEnvPath, which this
//     user may write, so the console's OLLAMA_* settings apply when it restarts.
//
// The file only sets environment variables for the ollama user's process; it can't change
// what runs or as whom, so it doesn't hand out root.
const (
	ServiceUnit    = "ollama"
	SudoersPath    = "/etc/sudoers.d/agent-machine-ollama"
	DropInPath     = "/etc/systemd/system/ollama.service.d/agent-machine.conf"
	ServiceEnvPath = "/etc/agent-machine/ollama.env"
	// Commands to run instead of systemctl and sudo, as JSON lists of arguments (tests use fakes).
	SystemctlEnv  = "AI_PROXY_SYSTEMCTL_COMMAND"
	ServiceCtlEnv = "AI_PROXY_SERVICE_COMMAND"
	ServiceEnvEnv = "AI_PROXY_SERVICE_ENV"
)

func serviceEnvPath() string {
	if path := os.Getenv(ServiceEnvEnv); path != "" {
		return path
	}
	return ServiceEnvPath
}

// systemctl is the systemctl to read state with (no privileges needed).
func systemctl() []string {
	return command(SystemctlEnv, func() string {
		if runtime.GOOS != "linux" {
			return ""
		}
		found, _ := exec.LookPath("systemctl")
		return found
	})
}

// serviceControl is how this user starts, stops and restarts the service: through sudo,
// without a password, as the sudoers rule allows (-n: never ask, fail instead).
func serviceControl() []string {
	if value := os.Getenv(ServiceCtlEnv); value != "" {
		var parts []string
		if json.Unmarshal([]byte(value), &parts) == nil && len(parts) > 0 {
			return parts
		}
		return nil
	}
	ctl := systemctl()
	if ctl == nil {
		return nil
	}
	return append([]string{"sudo", "-n"}, ctl...)
}

func run(timeout time.Duration, program []string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, program[0], append(program[1:], args...)...)
	process.Quiet(cmd)
	var output bytes.Buffer
	cmd.Stdout, cmd.Stderr = &output, &output
	err := cmd.Run()
	return strings.TrimSpace(output.String()), err
}

// ollamaService is whether Ollama here is a systemd service.
func ollamaService() bool {
	ctl := systemctl()
	if ctl == nil {
		return false
	}
	state, err := run(5*time.Second, ctl, "show", ServiceUnit, "-p", "LoadState", "--value")
	return err == nil && state == "loaded"
}

// serviceAllowed is whether `sudo agent-machine servers service` has been run: this user may
// restart it without a password, and may write the environment it starts with.
func serviceAllowed() bool {
	control := serviceControl()
	if control == nil {
		return false
	}
	if os.Getenv(ServiceCtlEnv) == "" {
		// sudo -n -l <command> answers 0 only when that exact command is allowed without a password.
		if _, err := run(5*time.Second, []string{"sudo", "-n", "-l"}, append(systemctl(), "restart", ServiceUnit)...); err != nil {
			return false
		}
	}
	file, err := os.OpenFile(serviceEnvPath(), os.O_WRONLY|os.O_APPEND, 0)
	if err != nil {
		return false
	}
	file.Close()
	return true
}

const serviceHint = "Ollama here is the systemd service ollama.service, which agent-machine can't restart yet: run `sudo agent-machine servers service` there once to let it"

// serviceAction starts, stops or restarts the service; start and restart write its settings first.
func (m *Manager) serviceAction(op, bind string) error {
	if !serviceAllowed() {
		return errors.New(serviceHint)
	}
	if op != "stop" {
		config := LoadConfig()
		env := ManagedEnv(config)
		if err := writeServiceEnv(env, fmt.Sprintf("%s:%d", bind, m.port())); err != nil {
			return err
		}
		if err := (state{OllamaBind: bind, OllamaEnv: publicEnv(env), Service: true}).save(); err != nil {
			return err
		}
	}
	if output, err := run(90*time.Second, serviceControl(), op, ServiceUnit); err != nil {
		return fmt.Errorf("systemctl %s %s: %v %s", op, ServiceUnit, err, output)
	}
	for deadline := time.Now().Add(30 * time.Second); time.Now().Before(deadline); time.Sleep(250 * time.Millisecond) {
		if up := m.ollamaVersion() != ""; up == (op != "stop") {
			return nil
		}
	}
	if op == "stop" {
		return errors.New("Ollama is still answering after its service stopped")
	}
	return errors.New("Ollama's service started but doesn't answer yet; see journalctl -u ollama")
}

// writeServiceEnv writes the environment the service starts with: the saved settings, then OLLAMA_HOST.
func writeServiceEnv(env map[string]*string, host string) error {
	names := make([]string, 0, len(env))
	for name := range env {
		names = append(names, name)
	}
	sort.Strings(names)
	var text strings.Builder
	text.WriteString("# Written by agent-machine from the console's Ollama settings; `sudo agent-machine servers service` set this up.\n")
	for _, name := range names {
		if value := env[name]; value != nil {
			fmt.Fprintf(&text, "%s=%s\n", name, quoteEnv(*value))
		}
	}
	fmt.Fprintf(&text, "OLLAMA_HOST=%s\n", quoteEnv(host))
	return os.WriteFile(serviceEnvPath(), []byte(text.String()), 0o644)
}

// quoteEnv quotes a value for a systemd EnvironmentFile.
func quoteEnv(value string) string {
	value = strings.NewReplacer(`\`, `\\`, `"`, `\"`, "\n", " ").Replace(value)
	return `"` + value + `"`
}

// SetUpService is `sudo agent-machine servers service`: lets user restart ollama.service and
// write its settings. off removes it all again.
func SetUpService(user string, off bool) error {
	if runtime.GOOS != "linux" {
		return errors.New("this is for an Ollama installed as a systemd service on Linux")
	}
	if os.Geteuid() != 0 {
		return errors.New("run it with sudo: sudo agent-machine servers service")
	}
	ctl := systemctl()
	if ctl == nil || !ollamaService() {
		return errors.New("there's no ollama.service here: agent-machine starts and stops this Ollama itself")
	}
	if off {
		for _, path := range []string{SudoersPath, DropInPath, ServiceEnvPath} {
			if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
				return err
			}
		}
		_, err := run(30*time.Second, ctl, "daemon-reload")
		return err
	}
	if user == "" || user == "root" || strings.ContainsAny(user, " \t\n:,=\\") {
		return errors.New("run it with sudo from your own account, so it knows whose agent-machine may restart Ollama")
	}
	systemctlPath := ctl[0]
	rule := fmt.Sprintf("# Written by agent-machine (sudo agent-machine servers service): only these three commands.\n"+
		"%[1]s ALL=(root) NOPASSWD: %[2]s start %[3]s, %[2]s stop %[3]s, %[2]s restart %[3]s\n", user, systemctlPath, ServiceUnit)
	candidate := SudoersPath + ".new"
	if err := os.WriteFile(candidate, []byte(rule), 0o440); err != nil {
		return err
	}
	if output, err := run(10*time.Second, []string{"visudo", "-cf"}, candidate); err != nil {
		os.Remove(candidate)
		return fmt.Errorf("the sudoers rule didn't check out: %v %s", err, output)
	}
	if err := os.Rename(candidate, SudoersPath); err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(DropInPath), 0o755); err != nil {
		return err
	}
	dropIn := "# Written by agent-machine (sudo agent-machine servers service): Ollama's settings from the console.\n" +
		"[Service]\nEnvironmentFile=-" + ServiceEnvPath + "\n"
	if err := os.WriteFile(DropInPath, []byte(dropIn), 0o644); err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(ServiceEnvPath), 0o755); err != nil {
		return err
	}
	if _, err := os.Stat(ServiceEnvPath); errors.Is(err, os.ErrNotExist) {
		if err := os.WriteFile(ServiceEnvPath, []byte("# agent-machine writes Ollama's settings here.\n"), 0o644); err != nil {
			return err
		}
	}
	if output, err := run(10*time.Second, []string{"chown", user}, ServiceEnvPath); err != nil {
		return fmt.Errorf("chown %s: %v %s", ServiceEnvPath, err, output)
	}
	if output, err := run(30*time.Second, ctl, "daemon-reload"); err != nil {
		return fmt.Errorf("systemctl daemon-reload: %v %s", err, output)
	}
	return nil
}
