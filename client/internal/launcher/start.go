package launcher

import (
	"fmt"
	"net/url"
	"os"
	"os/exec"
	"strconv"
	"time"

	"ai-coding-agent-proxy/client/internal/api"
	"ai-coding-agent-proxy/client/internal/process"
	"ai-coding-agent-proxy/client/internal/settings"
)

// EnsureProxy starts this machine's agent-proxy if it isn't running, and the agent-server
// too when it's this computer and down. It returns the agent-proxy's /health: its server
// and sign-in.
func EnsureProxy() (map[string]any, error) {
	local := settings.ProxyURL()
	status := api.Health(local)
	if status == nil {
		self, err := os.Executable()
		if err != nil {
			return nil, err
		}
		if err := start(self, []string{"proxy", "--port", strconv.Itoa(settings.ProxyPort())}, local, "agent-proxy", "agent proxy", 30*time.Second); err != nil {
			return nil, err
		}
		status = api.Health(local)
		if status == nil {
			return nil, fmt.Errorf("agent-proxy didn't answer at %s", local)
		}
	}
	if status["role"] != "agent-proxy" {
		return nil, fmt.Errorf("something other than agent-proxy answers on %s; set AI_PROXY_LOCAL_PORT to a free port", local)
	}
	server, _ := status["server"].(string)
	defer versionNotices(status, server)
	if settings.IsLoopbackURL(server) && api.Health(server) == nil {
		// One computer for everything: start the agent-server as well, if it's installed here.
		program, err := exec.LookPath("agent-server")
		if err != nil {
			return nil, fmt.Errorf("the agent-server at %s isn't running; start it (agent-server, or its Docker container), or sign in to another with `agent login <server>`", server)
		}
		parsed, _ := url.Parse(server)
		port := parsed.Port()
		if port == "" {
			port = "80"
			if parsed.Scheme == "https" {
				port = "443"
			}
		}
		// Generous: a fresh agent-server rebuilds the web UI first when its sources changed.
		if err := start(program, []string{"--port", port}, server, "agent-server", "agent-server", 120*time.Second); err != nil {
			return nil, err
		}
	}
	return status, nil
}

// versionNotices says, on stderr (a headless launch's stdout is its session line), when
// the running agent-proxy or the server isn't this agent's version. A build without a
// version (go build by hand) says nothing.
func versionNotices(status map[string]any, server string) {
	for _, line := range VersionNotices(settings.Version, status, api.Health(server)) {
		fmt.Fprintln(os.Stderr, "agent:", line)
	}
}

// VersionNotices compares this agent's version with the agent-proxy's /health and the server's.
func VersionNotices(own string, proxy, server map[string]any) []string {
	if own == "" || own == "dev" {
		return nil
	}
	var lines []string
	if running, _ := proxy["version"].(string); running != own {
		if running == "" {
			running = "an older version"
		}
		lines = append(lines, fmt.Sprintf("the running agent-proxy is %s; this agent is %s. Run `agent proxy stop` so the next agent starts this one.", running, own))
	}
	if theirs, _ := server["version"].(string); theirs != "" && theirs != own {
		lines = append(lines, fmt.Sprintf("agent-server is %s; this agent is %s. Update with: agent update", theirs, own))
	}
	return lines
}

// start runs program in the background, detached from this terminal, and waits until
// url/health answers.
func start(program string, args []string, url, name, manual string, timeout time.Duration) error {
	cmd := exec.Command(program, args...)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = nil, nil, nil
	process.Detach(cmd)
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("couldn't start %s: %v", name, err)
	}
	exited := make(chan struct{})
	go func() {
		_ = cmd.Wait()
		close(exited)
	}()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		select {
		case <-exited:
			return fmt.Errorf("%s exited while starting. Run `%s` in a terminal to see why", name, manual)
		default:
		}
		if api.Health(url) != nil {
			return nil
		}
		time.Sleep(200 * time.Millisecond)
	}
	_ = cmd.Process.Kill()
	return fmt.Errorf("%s didn't become ready at %s", name, url)
}

var containerPhases = map[string]string{
	"pulling":  "pulling its image",
	"starting": "starting the container",
	"loading":  "loading the model (the first start also downloads it)",
	"ready":    "ready",
}

func phase(name string) string {
	if text, ok := containerPhases[name]; ok {
		return text
	}
	return name
}

type containerStatus struct {
	Ready       bool   `json:"ready"`
	Phase       string `json:"phase"`
	EngineLabel string `json:"engine_label"`
	Job         struct {
		State string `json:"state"`
		Error string `json:"error"`
	} `json:"job"`
	State struct {
		Error string `json:"error"`
	} `json:"state"`
}

// EnsureContainer starts a backend's container if it isn't serving, and waits until it is.
func EnsureContainer(proxyURL, name string, switchOthers bool, wait time.Duration) error {
	client := api.New(proxyURL, 30*time.Second)
	path := "/api/containers/" + url.PathEscape(name)
	var item containerStatus
	if err := client.Get(path, &item); err != nil {
		if api.StatusOf(err) == 401 {
			return fmt.Errorf("the agent-server won't start %s for this machine; sign it in with `agent login`, or start %s on its Containers page", name, name)
		}
		return err
	}
	if item.Ready {
		return nil
	}
	if item.Phase != "pulling" && item.Phase != "starting" && item.Phase != "loading" {
		if err := client.Post(path+"/start", map[string]any{"switch": switchOthers}, nil); err != nil {
			if api.StatusOf(err) == 409 {
				return fmt.Errorf("%v Run again with --switch, or switch on the Containers page", err)
			}
			return err
		}
	}
	fmt.Printf("Starting %s (%s container). Ctrl+C stops waiting; the container keeps starting.\n", name, item.EngineLabel)
	began, said := time.Now(), time.Now()
	last := ""
	for time.Since(began) < wait {
		if err := client.Get(path, &item); err != nil {
			return err
		}
		if item.Phase != last {
			fmt.Printf("  %s: %s\n", name, phase(item.Phase))
			last, said = item.Phase, time.Now()
		} else if time.Since(said) > time.Minute {
			fmt.Printf("  %s: still %s (%d min)\n", name, phase(item.Phase), int(time.Since(began).Minutes()))
			said = time.Now()
		}
		if item.Ready {
			return nil
		}
		switch item.Phase {
		case "failed", "stopped", "unhealthy", "not_created":
			if item.Job.State != "running" {
				reason := item.Job.Error
				if reason == "" {
					reason = item.State.Error
				}
				if reason == "" {
					reason = item.Phase
				}
				return fmt.Errorf("%s didn't start: %s. Its log is on the Containers page", name, reason)
			}
		}
		time.Sleep(containerPoll)
	}
	return fmt.Errorf("%s still isn't ready after %s", name, wait)
}

// containerPoll is how often EnsureContainer checks; tests shorten it.
var containerPoll = 2 * time.Second
