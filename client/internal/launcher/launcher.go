// Package launcher is `agent`: start a coding agent on this machine, registered with this
// machine's agent-proxy.
//
// The launcher makes sure the agent-proxy runs (and, when this computer is the server
// too, the agent-server), asks the server for a session and the agent's environment, and
// runs the agent so every request goes to the agent-proxy, which carries it to the
// agent-server. The server owns the agent definitions (its config/agents.yaml) and
// renders them; the launcher only knows how to run what it's given. With --control the
// agent runs in a terminal shared with the server, to watch and type into from the
// console or a phone.
package launcher

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"time"

	"ai-coding-agent-proxy/client/internal/api"
	"ai-coding-agent-proxy/client/internal/process"
	"ai-coding-agent-proxy/client/internal/settings"
	"ai-coding-agent-proxy/client/internal/terminal"
)

// Agent is one of the server's agents, from GET /api/agents.
type Agent struct {
	ID         string `json:"id"`
	Name       string `json:"name"`
	Executable string `json:"executable"`
	Protocol   string `json:"protocol"`
	Install    string `json:"install"`
	// The install command for each environment (linux, macos, windows, wsl), when they differ.
	Installs       map[string]string `json:"installs"`
	Intercept      bool              `json:"intercept"`
	NeedsModel     bool              `json:"needs_model"`
	Backends       []string          `json:"backends"`
	DefaultBackend string            `json:"default_backend"`
}

// Prepared is the server's answer to POST /api/agents/<id>/sessions.
type Prepared struct {
	Session struct {
		SessionID string `json:"session_id"`
		Backend   string `json:"backend"`
	} `json:"session"`
	Env     map[string]string `json:"env"`
	Command []string          `json:"command"`
	// Files the agent reads (its settings for the session, such as the model's real context
	// size), for the launcher to write to the files_dir it named; the env points at them.
	Files     map[string]string `json:"files"`
	Container bool              `json:"container"`
	// The shared MCP servers the agent got.
	MCP []struct {
		ID string `json:"id"`
	} `json:"mcp"`
}

// Main runs `agent` with its arguments and returns the exit status.
func Main(args []string) int {
	options, err := ParseArgs(args)
	if err != nil {
		fmt.Fprintf(os.Stderr, "agent: %v\n\n%s", err, Usage)
		return 2
	}
	if options.Help {
		fmt.Print(Usage)
		return 0
	}
	if options.Proxy != "" {
		fmt.Fprintln(os.Stderr, "agent: --proxy is gone: sign this machine in to a server with `agent login <server>`, and its agent-proxy uses it")
		return 2
	}
	if options.Shell && !options.List {
		status, err := LaunchShell(options)
		if err != nil {
			fmt.Fprintf(os.Stderr, "agent: %v\n", err)
			return 1
		}
		return status
	}
	if options.List || options.Agent == "" {
		if err := List(); err != nil {
			fmt.Fprintf(os.Stderr, "agent: %v\n", err)
			return 1
		}
		if options.List {
			return 0
		}
		return 2
	}
	options.Control = options.Control || options.Headless || os.Getenv("AI_PROXY_CONTROL") == "1"
	if options.LetOthersType && !options.Control {
		fmt.Fprintln(os.Stderr, "agent: --let-others-type has no effect without --control")
	}
	status, err := Launch(options)
	if err != nil {
		fmt.Fprintf(os.Stderr, "agent: %v\n", err)
		return 1
	}
	return status
}

// Launch runs one agent in one session and returns its exit status.
func Launch(options Options) (int, error) {
	project, err := filepath.Abs(options.Project)
	if err != nil {
		return 1, err
	}
	if info, err := os.Stat(project); err != nil || !info.IsDir() {
		return 1, fmt.Errorf("project directory does not exist: %s", project)
	}
	status, err := EnsureProxy()
	if err != nil {
		return 1, err
	}
	server, _ := status["server"].(string)
	local := settings.ProxyURL()
	// Everything goes through this machine's agent-proxy, which signs it as this machine.
	client := api.New(local, 30*time.Second)
	if err := checkSignedIn(client, server); err != nil {
		return 1, err
	}
	agent, err := findAgent(client, options.Agent)
	if err != nil {
		return 1, err
	}
	executable, err := process.LookPath(agent.Executable)
	if err != nil {
		hint := ""
		if install := agent.installCommand(); install != "" {
			hint = " Install it with: " + install
		}
		return 1, fmt.Errorf("%s (%s) is not on PATH.%s", agent.Name, agent.Executable, hint)
	}
	if agent.NeedsModel && options.Model == "" {
		return 1, fmt.Errorf("%s needs --model", agent.Name)
	}
	request := map[string]any{
		"model": nullable(options.Model), "backend": nullable(options.Backend), "trace": options.Trace,
		"project": filepath.Base(project), "project_path": project, "proxy_url": local, "launcher": true,
	}
	if len(options.MCP) > 0 {
		request["mcp"] = options.MCP
	}
	for key, value := range options.Sharing() {
		request[key] = value
	}
	// Where it runs, and for a launch from the console the fleet machine (agent-machine sets
	// these) and the launch that started it, for the audit trail.
	tags := map[string]any{}
	if hostname, err := os.Hostname(); err == nil {
		// hostname stays the computer's name, so the server can put an agent started here by
		// hand under the fleet machine with the same hostname.
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
	request["tags"] = tags
	filesDir, err := sessionFilesDir()
	if err != nil {
		return 1, err
	}
	request["files_dir"] = filesDir
	var intercept map[string]any
	if agent.Intercept {
		if intercept, err = interceptFiles(client); err != nil {
			return 1, err
		}
		request["intercept"] = intercept
	}
	var prepared Prepared
	if err := client.Post("/api/agents/"+url.PathEscape(agent.ID)+"/sessions", request, &prepared); err != nil {
		return 1, err
	}
	sessionID := prepared.Session.SessionID
	end := func(exitStatus int) {
		if err := client.Patch("/api/sessions/"+url.PathEscape(sessionID), map[string]any{"ended": true, "exit_status": exitStatus}, nil); err != nil {
			fmt.Fprintf(os.Stderr, "Warning: could not close the session: %v\n", err)
		}
	}
	if len(prepared.Files) > 0 {
		defer os.RemoveAll(filesDir)
		if err := writeFiles(filesDir, prepared.Files); err != nil {
			end(1)
			return 1, err
		}
	}
	if prepared.Container {
		if err := EnsureContainer(local, prepared.Session.Backend, options.Switch, 4*time.Hour); err != nil {
			end(1)
			return 1, err
		}
	}
	if len(prepared.Command) == 0 {
		end(1)
		return 1, errors.New("the server sent no command for this agent")
	}
	// The resolved path, so npm .cmd shims work on Windows.
	command := append([]string{executable}, prepared.Command[1:]...)
	command = append(command, options.Extra...)
	env := mergeEnv(os.Environ(), prepared.Env)
	route := prepared.Session.Backend
	if intercept != nil {
		route = fmt.Sprintf("all HTTPS through %v (traffic goes to the agent's own service)", intercept["url"])
	}
	if options.Headless {
		return runHeadless(local, sessionID, agent.Executable, command, project, env, end), nil
	}
	share, controlLine := shareFor(local, server, sessionID, agent.Executable, options.Control)
	model := options.Model
	if model == "" {
		model = "agent default"
	}
	fmt.Printf("Session : %s\nAgent   : %s\nModel   : %s\nBackend : %s\nServer  : %s/live\nControl : %s\n",
		sessionID, agent.Name, model, route, server, controlLine)
	if len(prepared.MCP) > 0 {
		names := make([]string, 0, len(prepared.MCP))
		for _, item := range prepared.MCP {
			names = append(names, item.ID)
		}
		fmt.Println("MCP     :", strings.Join(names, ", "))
	}
	exitStatus := run(command, project, env, share)
	if share != nil && share.Problem() != "" {
		fmt.Fprintf(os.Stderr, "Control : %s\n", share.Problem())
	}
	end(exitStatus)
	return exitStatus, nil
}

// runHeadless runs a launch the server asked for: it reports the session on its one line of
// output (the agent-proxy reads it), then runs the agent with only the server's viewers
// watching and typing.
func runHeadless(local, sessionID, name string, command []string, dir string, env []string, end func(int)) int {
	if problem := terminal.Probe(local); strings.Contains(problem, "agent login") {
		fmt.Fprintln(os.Stderr, problem)
		end(1)
		return 1
	}
	share := terminal.NewShare(terminal.HostURL(local, sessionID), name)
	opened, _ := json.Marshal(map[string]string{"session_id": sessionID})
	fmt.Println(string(opened))
	// Nothing else goes to standard output: the agent-proxy may stop reading it.
	if null, err := os.OpenFile(os.DevNull, os.O_WRONLY, 0); err == nil {
		os.Stdout = null
	}
	status, err := terminal.RunHeadless(command, dir, env, share)
	if err != nil {
		fmt.Fprintf(os.Stderr, "agent: %v\n", err)
	}
	end(status)
	return status
}

// run runs the agent here, in a shared terminal when share isn't nil, and returns its exit status.
func run(command []string, dir string, env []string, share *terminal.Share) int {
	// Ctrl+C belongs to the agent; the launcher stays to close the session afterwards.
	interrupts := make(chan os.Signal, 1)
	signal.Notify(interrupts, os.Interrupt)
	defer signal.Stop(interrupts)
	if share != nil {
		status, err := terminal.Run(command, dir, env, share)
		if err != nil {
			fmt.Fprintf(os.Stderr, "agent: %v\n", err)
		}
		return status
	}
	cmd := exec.Command(command[0], command[1:]...)
	cmd.Dir, cmd.Env = dir, env
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	err := cmd.Run()
	var exit *exec.ExitError
	switch {
	case err == nil:
		return 0
	case errors.As(err, &exit):
		if code := exit.ExitCode(); code >= 0 {
			return code
		}
		return 1
	default:
		fmt.Fprintf(os.Stderr, "agent: %v\n", err)
		return 127
	}
}

func checkSignedIn(client *api.Client, server string) error {
	var status struct {
		Required bool `json:"required"`
		Identity any  `json:"identity"`
	}
	if err := client.Get("/api/auth/status", &status); err != nil {
		return fmt.Errorf("couldn't reach the agent-server at %s: %v", server, err)
	}
	if status.Required && status.Identity == nil {
		return fmt.Errorf("%s needs this machine to sign in: run `agent login %s`", server, server)
	}
	return nil
}

func agents(client *api.Client) ([]Agent, error) {
	var listed []Agent
	if err := client.Get("/api/agents", &listed); err != nil {
		if api.StatusOf(err) == 401 {
			return nil, fmt.Errorf("the agent-server needs this machine to sign in: run `agent login`")
		}
		return nil, err
	}
	return listed, nil
}

func findAgent(client *api.Client, id string) (Agent, error) {
	listed, err := agents(client)
	if err != nil {
		return Agent{}, err
	}
	var ids []string
	for _, agent := range listed {
		if agent.ID == id {
			return agent, nil
		}
		ids = append(ids, agent.ID)
	}
	sort.Strings(ids)
	available := strings.Join(ids, ", ")
	if available == "" {
		available = "none"
	}
	return Agent{}, fmt.Errorf("unknown agent '%s'. Available: %s", id, available)
}

// interceptFiles is the HTTPS proxy account agents use (this machine's agent-proxy) and
// local copies of the server's CA, kept per fingerprint so a server with a new CA gets
// a fresh copy.
func interceptFiles(client *api.Client) (map[string]any, error) {
	var info struct {
		Running     bool   `json:"running"`
		Error       string `json:"error"`
		URL         string `json:"url"`
		Fingerprint string `json:"ca_fingerprint"`
	}
	if err := client.Get("/api/intercept", &info); err != nil {
		return nil, err
	}
	if !info.Running {
		reason := info.Error
		if reason == "" {
			reason = "unknown error"
		}
		return nil, fmt.Errorf("the agent-server's HTTPS interception is off: %s", reason)
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return nil, err
	}
	fingerprint := strings.ReplaceAll(info.Fingerprint, ":", "")
	if len(fingerprint) > 16 {
		fingerprint = fingerprint[:16]
	}
	directory := filepath.Join(home, ".cache", "agent-proxy", fingerprint)
	caPath, bundlePath := filepath.Join(directory, "ca.pem"), filepath.Join(directory, "bundle.pem")
	if _, err := os.Stat(bundlePath); err != nil {
		if err := os.MkdirAll(directory, 0o755); err != nil {
			return nil, err
		}
		for path, source := range map[string]string{caPath: "/api/intercept/ca.pem", bundlePath: "/api/intercept/bundle.pem"} {
			pem, err := client.Text(source)
			if err != nil {
				return nil, fmt.Errorf("couldn't fetch the server's CA (%s): %v", source, err)
			}
			if err := writeFile(path, pem); err != nil {
				return nil, err
			}
		}
	}
	return map[string]any{"url": info.URL, "ca_path": caPath, "bundle_path": bundlePath}, nil
}

// sessionFilesDir is a new folder name for one launch's files, under the user's cache. It's
// made only when the server sends files.
func sessionFilesDir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	random := make([]byte, 8)
	if _, err := rand.Read(random); err != nil {
		return "", err
	}
	return filepath.Join(home, ".cache", "agent-proxy", "sessions", hex.EncodeToString(random)), nil
}

// writeFiles writes the server's files into dir, refusing any name that isn't a plain file name.
func writeFiles(dir string, files map[string]string) error {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	for name, text := range files {
		if name == "" || name != filepath.Base(name) || strings.ContainsAny(name, `/\:`) || name == "." || name == ".." {
			return fmt.Errorf("the server sent a file with an unsafe name: %q", name)
		}
		if err := writeFile(filepath.Join(dir, name), text); err != nil {
			return err
		}
	}
	return nil
}

func writeFile(path, text string) error {
	temporary := path + ".tmp"
	if err := os.WriteFile(temporary, []byte(text), 0o644); err != nil {
		return err
	}
	return os.Rename(temporary, path)
}

// shareFor sets up --control, and says how it went.
func shareFor(local, server, sessionID, name string, wanted bool) (*terminal.Share, string) {
	if !wanted {
		return nil, "off (add --control to watch and type into this agent from the console)"
	}
	if reason := terminal.Unavailable(); reason != "" {
		return nil, "off: " + reason
	}
	problem := terminal.Probe(local)
	if strings.Contains(problem, "agent login") {
		return nil, "off: " + problem
	}
	share := terminal.NewShare(terminal.HostURL(local, sessionID), name)
	view := terminal.ViewURL(server, sessionID)
	if problem != "" {
		return share, fmt.Sprintf("%s (%s)", view, problem)
	}
	return share, view + " (watch and type into this terminal from the console)"
}

// mergeEnv is base with overrides applied; names compare case-insensitively on Windows.
func mergeEnv(base []string, overrides map[string]string) []string {
	fold := func(name string) string {
		if runtime.GOOS == "windows" {
			return strings.ToUpper(name)
		}
		return name
	}
	var merged []string
	for _, entry := range base {
		name, _, _ := strings.Cut(entry, "=")
		if _, replaced := findFolded(overrides, fold(name), fold); replaced && name != "" {
			continue
		}
		merged = append(merged, entry)
	}
	names := make([]string, 0, len(overrides))
	for name := range overrides {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		merged = append(merged, name+"="+overrides[name])
	}
	return merged
}

func findFolded(values map[string]string, folded string, fold func(string) string) (string, bool) {
	for name, value := range values {
		if fold(name) == folded {
			return value, true
		}
	}
	return "", false
}

func nullable(value string) any {
	if value == "" {
		return nil
	}
	return value
}

// List prints the server's agents, whether each is installed here, and the agent-proxy's state.
func List() error {
	status, proxyErr := EnsureProxy()
	if proxyErr != nil {
		fmt.Printf("agent-proxy: not running (%v)\n", proxyErr)
		return nil
	}
	server, _ := status["server"].(string)
	listed, err := agents(api.New(settings.ProxyURL(), 15*time.Second))
	if err != nil {
		fmt.Printf("Couldn't list the agents of %s: %v\n", server, err)
	} else {
		width := 5
		for _, agent := range listed {
			width = max(width, len(agent.ID))
		}
		for _, agent := range listed {
			_, missing := process.LookPath(agent.Executable)
			state := "installed"
			if missing != nil {
				state = "missing  "
			}
			route := agent.Protocol
			if agent.Intercept {
				route = "account, captured via HTTPS intercept"
			}
			fmt.Printf("%-*s  %s  %s (%s)\n", width, agent.ID, state, agent.Name, route)
			if install := agent.installCommand(); missing != nil && install != "" {
				fmt.Printf("%-*s             install: %s\n", width, "", install)
			}
		}
	}
	who := "no sign-in stored (agent login <server>)"
	if user, _ := status["user"].(string); user != "" {
		who = "signed in as " + user
	}
	fmt.Printf("\nagent-proxy: carrying traffic to %s, %s\n", server, who)
	return nil
}

// installCommand is how to install the agent here: this environment's command, else the general one.
func (a Agent) installCommand() string {
	if command := a.Installs[process.Environment()]; command != "" {
		return command
	}
	return a.Install
}
