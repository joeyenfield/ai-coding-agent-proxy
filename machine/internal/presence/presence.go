// Package presence is this machine's membership of the server's fleet: the connection
// `agent-machine run` keeps open (/api/machines/connect) to say what the machine is and
// has (the LLM providers it runs above all), and the launches, container requests and
// questions about its model servers (package providers) the server sends over it.
//
// Launches run a program here, so they're off until `agent-machine launches on`, only go
// to folders listed with `agent-machine roots add`, and are checked here again whatever
// the server says. They run the agent command (`agent`) from PATH. Containers the server
// asks for (package containers) are off the same way, until `agent-machine containers on`.
package presence

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"

	"ai-coding-agent-proxy/machine/internal/containers"
	"ai-coding-agent-proxy/machine/internal/mcp"
	"ai-coding-agent-proxy/machine/internal/process"
	"ai-coding-agent-proxy/machine/internal/providers"
	"ai-coding-agent-proxy/machine/internal/servers"
	"ai-coding-agent-proxy/machine/internal/settings"
)

const (
	// How often the machine resends what it has (agents installed, model servers running).
	updateEvery = 60 * time.Second
	pingEvery   = 25 * time.Second
	// How long a launched agent may take to open its session.
	launchTimeout = 50 * time.Second
)

var retryDelays = []time.Duration{2 * time.Second, 5 * time.Second, 10 * time.Second, 30 * time.Second}

// Dialer opens the WebSocket to path on server, signed as this machine.
type Dialer func(server, path string) (*websocket.Conn, *http.Response, error)

// Presence keeps this machine connected to the server's fleet while `agent-machine run` runs.
type Presence struct {
	Upstream settings.Upstream
	// Dial defaults to Dial with Upstream's token.
	Dial   Dialer
	Logger *log.Logger
	// Launch starts a launch; tests replace it. By default it runs `agent` headless.
	Launch func(request Launch) (string, error)
	// Shell opens a shell for the owner (agent-machine commands on); tests replace it. By default
	// it runs `agent --shell` headless.
	Shell func(request Launch) (string, error)
	// SpawnUpdate starts `agent-machine <args>` detached for a remote update; tests replace it.
	SpawnUpdate func(args []string) error
	// OnStatus hears every change of the connection's state (agent-machine writes status.json).
	OnStatus func(status string)
	// OnServerVersion hears the server's version each time the machine connects.
	OnServerVersion func(version string)
	// Detect finds the model servers running here; tests replace it.
	Detect func() []Provider
	// Containers runs the server's containers here, when the machine allows it.
	Containers *containers.Manager
	// Servers starts and stops the Ollama and Strata installed here, when the machine allows it.
	Servers *servers.Manager
	// Providers answers the server's questions about the model servers here, and changes their models when allowed.
	Providers *providers.Manager
	// MCP shares the MCP servers listed in mcp.json (agent-machine mcp add) through the tunnel.
	MCP *mcp.Bridge

	mu     sync.Mutex
	status string
	// The ports tunnels may reach: the model servers in the last hello or update.
	tunnels tunnelPorts
}

// Status is how the connection is doing.
func (p *Presence) Status() string {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.status
}

func (p *Presence) setStatus(status string) {
	p.mu.Lock()
	p.status = status
	p.mu.Unlock()
	if p.OnStatus != nil {
		p.OnStatus(status)
	}
}

// Dial opens the WebSocket to path on server, with the machine's token. It never uses an
// environment proxy, like every connection agent-machine makes.
func Dial(upstream settings.Upstream, server, path string) (*websocket.Conn, *http.Response, error) {
	parsed, err := url.Parse(server)
	if err != nil {
		return nil, nil, err
	}
	scheme := "ws"
	if parsed.Scheme == "https" {
		scheme = "wss"
	}
	target := fmt.Sprintf("%s://%s%s%s", scheme, parsed.Host, strings.TrimRight(parsed.Path, "/"), path)
	header := http.Header{}
	settings.Identify(header)
	if token := upstream.Token(server); token != "" {
		header.Set(settings.TokenHeader, token)
	}
	dialer := websocket.Dialer{Proxy: nil, HandshakeTimeout: 10 * time.Second}
	return dialer.Dial(target, header)
}

// Run connects and reconnects until ctx ends.
func (p *Presence) Run(ctx context.Context) {
	if p.Launch == nil {
		p.Launch = p.launchHeadless
	}
	if p.Shell == nil {
		p.Shell = p.shellHeadless
	}
	if p.Dial == nil {
		p.Dial = func(server, path string) (*websocket.Conn, *http.Response, error) {
			return Dial(p.Upstream, server, path)
		}
	}
	if p.Detect == nil {
		p.Detect = DetectProviders
	}
	if p.Containers == nil {
		p.Containers = &containers.Manager{}
	}
	if p.Servers == nil {
		p.Servers = &servers.Manager{}
	}
	if p.Providers == nil {
		p.Providers = &providers.Manager{}
	}
	if p.MCP == nil {
		p.MCP = &mcp.Bridge{Logger: p.Logger}
		defer p.MCP.Close()
	}
	for attempt := 0; ctx.Err() == nil; attempt++ {
		started := time.Now()
		err := p.session(ctx)
		if ctx.Err() != nil {
			return
		}
		if time.Since(started) > time.Minute {
			attempt = 0
		}
		p.setStatus(fmt.Sprintf("disconnected: %v", err))
		p.logf("fleet: %v", err)
		select {
		case <-ctx.Done():
			return
		case <-time.After(retryDelays[min(attempt, len(retryDelays)-1)]):
		}
	}
}

func (p *Presence) session(ctx context.Context) error {
	server := p.Upstream.Server()
	conn, response, err := p.Dial(server, "/api/machines/connect")
	if err != nil {
		if response != nil && response.StatusCode == http.StatusUpgradeRequired {
			body, _ := io.ReadAll(io.LimitReader(response.Body, 4096))
			return fmt.Errorf("the agent-server at %s can't take this agent-machine: %s", server, settings.Detail(body))
		}
		if response != nil {
			return fmt.Errorf("the agent-server at %s answered %d (run `agent-machine login %s`)", server, response.StatusCode, server)
		}
		return err
	}
	defer conn.Close()
	var writeMu sync.Mutex
	send := func(value any) error {
		data, _ := json.Marshal(value)
		writeMu.Lock()
		defer writeMu.Unlock()
		_ = conn.SetWriteDeadline(time.Now().Add(15 * time.Second))
		return conn.WriteMessage(websocket.TextMessage, data)
	}
	if err := send(p.describe(server, "hello")); err != nil {
		return err
	}
	_ = conn.SetReadDeadline(time.Now().Add(20 * time.Second))
	_, data, err := conn.ReadMessage()
	if err != nil {
		var closed *websocket.CloseError
		if errors.As(err, &closed) && closed.Code == CloseUpgradeRequired {
			return fmt.Errorf("the agent-server at %s can't take this agent-machine: %s", server, closed.Text)
		}
		if errors.As(err, &closed) {
			return fmt.Errorf("the agent-server refused this machine: %s", closed.Text)
		}
		return err
	}
	_ = conn.SetReadDeadline(time.Time{})
	var welcome struct {
		Type string `json:"type"`
	}
	if json.Unmarshal(data, &welcome) != nil || welcome.Type != "welcome" {
		return errors.New("unexpected answer from the agent-server")
	}
	p.setStatus("connected to " + server)
	p.logf("fleet: connected to %s", server)
	p.checkVersion(server)
	sessionCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	go func() {
		ping, update := time.NewTicker(pingEvery), time.NewTicker(updateEvery)
		defer ping.Stop()
		defer update.Stop()
		for {
			select {
			case <-sessionCtx.Done():
				_ = conn.Close()
				return
			case <-ping.C:
				_ = send(map[string]any{"type": "ping"})
			case <-update.C:
				_ = send(p.describe(server, "update"))
			}
		}
	}()
	for {
		_, data, err := conn.ReadMessage()
		if err != nil {
			return err
		}
		var kind struct {
			Type string `json:"type"`
		}
		if json.Unmarshal(data, &kind) != nil {
			continue
		}
		if kind.Type == "server" {
			var request servers.Request
			if json.Unmarshal(data, &request) == nil {
				go func() {
					_ = send(p.Servers.Handle(request))
					// A model server started or stopped: the server sees it in the providers at once.
					if request.Op != "status" {
						_ = send(p.describe(server, "update"))
					}
				}()
			}
			continue
		}
		if kind.Type == "tunnel" {
			var request Tunnel
			if json.Unmarshal(data, &request) == nil {
				go func() {
					if err := p.tunnel(server, request, p.Containers.Ports()); err != nil {
						p.logf("fleet: tunnel to port %d: %v", request.Port, err)
					}
				}()
			}
			continue
		}
		if kind.Type == "provider" {
			var request providers.Request
			if json.Unmarshal(data, &request) == nil {
				// A pull streams its progress until the model is in, which can take an hour.
				go func() {
					progress := func(update map[string]any) {
						_ = send(map[string]any{"type": "provider_progress", "request": request.ID, "progress": update})
					}
					_ = send(p.Providers.Handle(request, progress))
				}()
			}
			continue
		}
		if kind.Type == "container" {
			var request containers.Request
			if json.Unmarshal(data, &request) == nil {
				// Starts answer at once (the job runs on); removes and logs can take a while.
				go func() { _ = send(p.Containers.Handle(request)) }()
			}
			continue
		}
		if kind.Type == "update" {
			var request UpdateRequest
			if json.Unmarshal(data, &request) == nil {
				go func() { _ = send(p.RemoteUpdate(server, request)) }()
			}
			continue
		}
		if kind.Type == "folders" {
			var request FoldersRequest
			if json.Unmarshal(data, &request) == nil {
				go func() { _ = send(Folders(request, settings.LoadMachine())) }()
			}
			continue
		}
		var message Launch
		if json.Unmarshal(data, &message) != nil || (message.Type != "launch" && message.Type != "shell") {
			continue
		}
		go func(request Launch) {
			run, what := p.Launch, request.Agent
			if request.Type == "shell" {
				run, what = p.Shell, "a shell"
			}
			sessionID, err := run(request)
			if err != nil {
				p.logf("fleet: launch %s of %s in %s failed: %v", request.ID, what, request.Project, err)
				_ = send(map[string]any{"type": "launch_failed", "launch": request.ID, "error": err.Error()})
				return
			}
			p.logf("fleet: launched %s in %s: session %s", what, request.Project, sessionID)
			_ = send(map[string]any{"type": "launched", "launch": request.ID, "session_id": sessionID})
			// What's installed may have changed the server's view; send it again soon.
			_ = send(p.describe(server, "update"))
		}(message)
	}
}

// CloseUpgradeRequired is how the server closes the fleet socket of a machine whose protocol it doesn't take.
const CloseUpgradeRequired = 4426

// checkVersion logs, once per connection, when the server runs another version, and keeps
// its version for agent-machine's status display. A build without a version says nothing.
func (p *Presence) checkVersion(server string) {
	theirs := ServerVersion(server)
	if p.OnServerVersion != nil {
		p.OnServerVersion(theirs)
	}
	if notice := VersionNotice(settings.Version, theirs); notice != "" {
		p.logf("fleet: %s", notice)
	}
}

// ServerVersion is the version in the server's public /health, or "" when it doesn't say.
func ServerVersion(server string) string {
	response, err := settings.ServerClient(5 * time.Second).Get(strings.TrimRight(server, "/") + "/health")
	if err != nil {
		return ""
	}
	defer response.Body.Close()
	var health struct {
		Version string `json:"version"`
	}
	if response.StatusCode != http.StatusOK || json.NewDecoder(response.Body).Decode(&health) != nil {
		return ""
	}
	return health.Version
}

// VersionNotice is the line to show when the server's version isn't this agent-machine's.
func VersionNotice(own, server string) string {
	if own == "" || own == "dev" || server == "" || server == own {
		return ""
	}
	return fmt.Sprintf("agent-server is %s; this agent-machine is %s. Update with: agent-machine update", server, own)
}

// describe is the machine's hello or update: what it is, has and allows.
func (p *Presence) describe(server, kind string) map[string]any {
	machine := settings.LoadMachine()
	serving := p.Servers.Describe()
	found := withKeys(withStrata(p.Detect(), serving))
	ports := make([]int, 0, len(found))
	for _, provider := range found {
		ports = append(ports, provider.Port)
	}
	// The MCP servers it shares, by name; the server reaches them on the bridge's port, through the tunnel.
	var shared map[string]any
	if p.MCP != nil {
		shared = p.MCP.Describe()
	}
	if shared != nil {
		ports = append(ports, shared["port"].(int))
	}
	p.tunnels.set(ports)
	// Agents started here by hand tag their sessions with it, which puts them under this machine.
	hostname, _ := os.Hostname()
	return map[string]any{
		"type": kind, "id": machine.ID, "name": machine.DisplayName(), "hostname": hostname, "os": runtime.GOOS, "arch": runtime.GOARCH,
		"version": settings.Version, "protocol": settings.Protocol, "address": machine.Address, "agents": p.installedAgents(server),
		"providers": found, "launches": machine.Launches, "roots": machine.Roots, "commands": machine.Commands, "updates": machine.Updates,
		// It carries agents' requests to its model servers itself, so the server needn't reach them (tunnel.go).
		"tunnel": 1,
		// Launches run the agent command: say whether it's here, so the console can say what's missing.
		"launcher":   launcherFound(),
		"containers": p.Containers.Describe(), "servers": serving, "mcp": shared,
	}
}

// installedAgents is which of the server's agents have their executable on this machine's PATH.
func (p *Presence) installedAgents(server string) []string {
	request, err := http.NewRequest(http.MethodGet, server+"/api/agents", nil)
	if err != nil {
		return nil
	}
	if token := p.Upstream.Token(server); token != "" {
		request.Header.Set(settings.TokenHeader, token)
	}
	response, err := settings.ServerClient(10 * time.Second).Do(request)
	if err != nil {
		return nil
	}
	defer response.Body.Close()
	var agents []struct {
		ID         string `json:"id"`
		Executable string `json:"executable"`
	}
	if response.StatusCode != http.StatusOK || json.NewDecoder(response.Body).Decode(&agents) != nil {
		return nil
	}
	installed := []string{}
	for _, agent := range agents {
		if _, err := process.LookPath(agent.Executable); err == nil {
			installed = append(installed, agent.ID)
		}
	}
	return installed
}

// Launch is the server's request to start an agent here.
type Launch struct {
	Type       string `json:"type"`
	ID         string `json:"launch"`
	Agent      string `json:"agent"`
	Project    string `json:"project"`
	Model      string `json:"model"`
	Backend    string `json:"backend"`
	Trace      bool   `json:"trace"`
	Visibility string `json:"visibility"`
	Team       string `json:"team"`
	Access     string `json:"access"`
	// MCP names the shared MCP servers the agent gets ("<machine>/<name>"), which the server checked.
	MCP []string `json:"mcp"`
}

// Check is the machine's own say over a launch, whatever the server allowed: launches on,
// and the folder inside one of the roots. It returns the folder resolved.
func Check(request Launch, machine *settings.Machine) (string, error) {
	if !machine.Launches {
		return "", errors.New("this machine doesn't take launches (agent-machine launches on)")
	}
	return inRoots(request.Project, machine)
}

// CheckShell is the machine's own say over a shell: commands on, and the folder (which has
// to exist) inside one of the roots. It returns the folder resolved.
func CheckShell(request Launch, machine *settings.Machine) (string, error) {
	if !machine.Commands {
		return "", errors.New("this machine doesn't take commands (agent-machine commands on)")
	}
	if _, missing, err := existingParent(request.Project); err != nil || len(missing) > 0 {
		return "", fmt.Errorf("%s isn't a folder here", request.Project)
	}
	return inRoots(request.Project, machine)
}

// inRoots resolves a folder inside one of the roots, making the part that doesn't exist yet.
func inRoots(folder string, machine *settings.Machine) (string, error) {
	request := Launch{Project: folder}
	// A new project folder is made, inside a launch folder: the part that exists is resolved
	// (links followed) and checked, and only plain names are added to it.
	existing, missing, err := existingParent(request.Project)
	if err != nil {
		return "", err
	}
	base, ok := machine.Within(existing)
	if !ok {
		return "", fmt.Errorf("%s isn't inside one of this machine's launch folders", request.Project)
	}
	project := filepath.Join(append([]string{base}, missing...)...)
	if len(missing) > 0 {
		if err := os.MkdirAll(project, 0o755); err != nil {
			return "", fmt.Errorf("couldn't create %s: %v", request.Project, err)
		}
	}
	if info, err := os.Stat(project); err != nil || !info.IsDir() {
		return "", fmt.Errorf("%s isn't a folder", request.Project)
	}
	return project, nil
}

// existingParent splits a folder into its deepest part that exists and the names below it that don't.
func existingParent(dir string) (string, []string, error) {
	if dir == "" {
		return "", nil, errors.New("name a folder")
	}
	current, err := filepath.Abs(dir)
	if err != nil {
		return "", nil, err
	}
	var missing []string
	for {
		if _, err := os.Lstat(current); err == nil {
			return current, missing, nil
		}
		parent := filepath.Dir(current)
		name := filepath.Base(current)
		if parent == current || name == ".." || name == "." {
			return "", nil, fmt.Errorf("%s isn't inside one of this machine's launch folders", dir)
		}
		missing = append([]string{name}, missing...)
		current = parent
	}
}

// Arguments is the `agent` command line for a launch, run headless with its terminal shared.
func Arguments(request Launch, project string) []string {
	args := []string{request.Agent, project, "--headless", "--launch-id", request.ID, "--share", visibility(request.Visibility)}
	if request.Model != "" {
		args = append(args, "--model", request.Model)
	}
	if request.Backend != "" {
		args = append(args, "--backend", request.Backend)
	}
	if request.Trace {
		args = append(args, "--trace")
	}
	if request.Team != "" {
		args = append(args, "--team", request.Team)
	}
	if request.Access == "control" {
		args = append(args, "--let-others-type")
	}
	if len(request.MCP) > 0 {
		args = append(args, "--mcp", strings.Join(request.MCP, ","))
	}
	return args
}

func visibility(value string) string {
	if value == "team" || value == "org" {
		return value
	}
	return "private"
}

func launcherFound() bool {
	_, err := AgentCommand()
	return err == nil
}

// AgentCommand is the agent command launches run: AI_PROXY_AGENT, or `agent` on PATH.
func AgentCommand() (string, error) {
	if program := os.Getenv("AI_PROXY_AGENT"); program != "" {
		return program, nil
	}
	program, err := exec.LookPath("agent")
	if err != nil {
		return "", errors.New("the agent command isn't on this machine's PATH; install the client to take launches")
	}
	return program, nil
}

// agentProxyServer is the server this computer's agent-proxy carries traffic to, when it runs.
func agentProxyServer() string {
	port := "8184"
	if value, err := strconv.Atoi(os.Getenv("AI_PROXY_LOCAL_PORT")); err == nil && value > 0 {
		port = strconv.Itoa(value)
	}
	response, err := settings.Client(2 * time.Second).Get("http://127.0.0.1:" + port + "/health")
	if err != nil {
		return ""
	}
	defer response.Body.Close()
	var health struct {
		Role   string `json:"role"`
		Server string `json:"server"`
	}
	if json.NewDecoder(response.Body).Decode(&health) != nil || health.Role != "agent-proxy" {
		return ""
	}
	return health.Server
}

// launchHeadless runs `agent ... --headless` detached, and reads the session it opens from
// its first line of output. The agent goes through this computer's agent-proxy, signed in
// with `agent login`, so that has to be the server this machine supplies too.
func (p *Presence) launchHeadless(request Launch) (string, error) {
	machine := settings.LoadMachine()
	project, err := Check(request, machine)
	if err != nil {
		return "", err
	}
	return p.runHeadless(machine, project, Arguments(request, project))
}

// shellHeadless runs `agent --shell <folder> --headless`: the user's shell in a terminal only the
// machine's owner sees, for a remote terminal from the console.
func (p *Presence) shellHeadless(request Launch) (string, error) {
	machine := settings.LoadMachine()
	project, err := CheckShell(request, machine)
	if err != nil {
		return "", err
	}
	return p.runHeadless(machine, project, ShellArguments(request, project))
}

// ShellArguments is the `agent` command line for a shell, headless and private.
func ShellArguments(request Launch, project string) []string {
	return []string{"--shell", project, "--headless", "--launch-id", request.ID}
}

// runHeadless runs the agent command with args in project, detached, and returns the session
// it reports on its first line of output.
func (p *Presence) runHeadless(machine *settings.Machine, project string, args []string) (string, error) {
	program, err := AgentCommand()
	if err != nil {
		return "", err
	}
	server := p.Upstream.Server()
	if running := agentProxyServer(); running != "" && settings.Normalize(running) != settings.Normalize(server) {
		return "", fmt.Errorf("the agent-proxy here carries traffic to %s, not %s; sign the agent command in there (agent login %s)", running, server, server)
	}
	cmd := exec.Command(program, args...)
	cmd.Dir = project
	// AI_PROXY_URL points a newly started agent-proxy at this machine's server; the machine
	// variables tag the session with the machine it runs on.
	cmd.Env = append(os.Environ(), "AI_PROXY_URL="+server, "AI_PROXY_MACHINE_ID="+machine.ID, "AI_PROXY_MACHINE_NAME="+machine.DisplayName())
	process.Detach(cmd)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return "", err
	}
	var stderr strings.Builder
	cmd.Stderr = &limited{builder: &stderr, limit: 4096}
	if err := cmd.Start(); err != nil {
		return "", err
	}
	exited := make(chan error, 1)
	go func() { exited <- cmd.Wait() }()
	lines := make(chan string, 1)
	go func() {
		scanner := bufio.NewScanner(stdout)
		if scanner.Scan() {
			lines <- scanner.Text()
		}
		close(lines)
		// Keep draining, so the agent never blocks on a full pipe.
		for scanner.Scan() {
		}
	}()
	select {
	case line, ok := <-lines:
		var opened struct {
			SessionID string `json:"session_id"`
		}
		if ok && json.Unmarshal([]byte(line), &opened) == nil && opened.SessionID != "" {
			_ = cmd.Process.Release()
			return opened.SessionID, nil
		}
		<-exited
		return "", fmt.Errorf("the agent didn't start: %s", strings.TrimSpace(stderr.String()))
	case err := <-exited:
		return "", fmt.Errorf("the agent exited (%v): %s", err, strings.TrimSpace(stderr.String()))
	case <-time.After(launchTimeout):
		_ = cmd.Process.Kill()
		return "", errors.New("the agent didn't open a session in time")
	}
}

func (p *Presence) logf(format string, args ...any) {
	if p.Logger != nil {
		p.Logger.Printf(format, args...)
	}
}

// limited keeps the first limit bytes written to it.
type limited struct {
	mu      sync.Mutex
	builder *strings.Builder
	limit   int
}

func (l *limited) Write(data []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if room := l.limit - l.builder.Len(); room > 0 {
		l.builder.Write(data[:min(room, len(data))])
	}
	return len(data), nil
}
