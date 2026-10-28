package launcher

import (
	"errors"
	"fmt"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"time"

	"ai-coding-agent-proxy/client/internal/api"
	"ai-coding-agent-proxy/client/internal/settings"
	"ai-coding-agent-proxy/client/internal/terminal"
)

// ShellClient is the client a shell's session names; the server keeps those sessions private.
const ShellClient = "shell"

// LaunchShell runs the user's shell in a session of its own, its terminal shared with the server
// for its owner only: `agent --shell [folder]` here, and the terminals agent-machine opens from
// the console (headless). It sends no model traffic; the session is there for the terminal.
func LaunchShell(options Options) (int, error) {
	project, err := filepath.Abs(options.Project)
	if err != nil {
		return 1, err
	}
	if info, err := os.Stat(project); err != nil || !info.IsDir() {
		return 1, fmt.Errorf("folder does not exist: %s", project)
	}
	if !options.Headless {
		if reason := terminal.Unavailable(); reason != "" {
			return 1, fmt.Errorf("a shared shell needs a terminal here: %s", reason)
		}
	}
	command, err := ShellCommand()
	if err != nil {
		return 1, err
	}
	status, err := EnsureProxy()
	if err != nil {
		return 1, err
	}
	server, _ := status["server"].(string)
	local := settings.ProxyURL()
	client := api.New(local, 30*time.Second)
	if err := checkSignedIn(client, server); err != nil {
		return 1, err
	}
	tags := map[string]any{"project_path": project}
	if hostname, err := os.Hostname(); err == nil {
		tags["machine_name"] = hostname
		tags["hostname"] = hostname
	}
	if id := os.Getenv("AI_PROXY_MACHINE_ID"); id != "" {
		tags["machine"] = id
	}
	if name := os.Getenv("AI_PROXY_MACHINE_NAME"); name != "" {
		tags["machine_name"] = name
	}
	if options.LaunchID != "" {
		tags["launch"] = options.LaunchID
	}
	var session struct {
		SessionID string `json:"session_id"`
	}
	request := map[string]any{"client": ShellClient, "backend": "direct", "project": filepath.Base(project), "visibility": "private", "access": "view", "tags": tags}
	if err := client.Post("/api/sessions", request, &session); err != nil {
		return 1, err
	}
	if session.SessionID == "" {
		return 1, errors.New("the server opened no session for the shell")
	}
	end := func(exitStatus int) {
		if err := client.Patch("/api/sessions/"+url.PathEscape(session.SessionID), map[string]any{"ended": true, "exit_status": exitStatus}, nil); err != nil {
			fmt.Fprintf(os.Stderr, "Warning: could not close the session: %v\n", err)
		}
	}
	name := filepath.Base(command[0])
	if options.Headless {
		return runHeadless(local, session.SessionID, name, command, project, os.Environ(), end), nil
	}
	share, controlLine := shareFor(local, server, session.SessionID, name, true)
	if share == nil {
		end(1)
		return 1, errors.New(controlLine)
	}
	fmt.Printf("Session : %s\nShell   : %s in %s\nControl : %s (only you see it)\n", session.SessionID, command[0], project, controlLine)
	exitStatus := run(command, project, os.Environ(), share)
	if share.Problem() != "" {
		fmt.Fprintf(os.Stderr, "Control : %s\n", share.Problem())
	}
	end(exitStatus)
	return exitStatus, nil
}

// ShellCommand is this user's shell: $SHELL (as a login shell) elsewhere, PowerShell on Windows
// (pwsh when it's installed), else %ComSpec%.
func ShellCommand() ([]string, error) {
	if runtime.GOOS == "windows" {
		for _, name := range []string{"pwsh.exe", "powershell.exe"} {
			if path, err := exec.LookPath(name); err == nil {
				return []string{path, "-NoLogo"}, nil
			}
		}
		if comspec := os.Getenv("ComSpec"); comspec != "" {
			return []string{comspec}, nil
		}
		return nil, errors.New("no shell found: neither PowerShell nor %ComSpec%")
	}
	for _, candidate := range []string{os.Getenv("SHELL"), "/bin/bash", "/bin/sh"} {
		if candidate == "" {
			continue
		}
		if path, err := exec.LookPath(candidate); err == nil {
			return []string{path, "-l"}, nil
		}
	}
	return nil, errors.New("no shell found: set SHELL")
}
