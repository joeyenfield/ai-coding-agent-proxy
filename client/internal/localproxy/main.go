package localproxy

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"time"

	"ai-coding-agent-proxy/client/internal/api"
	"ai-coding-agent-proxy/client/internal/settings"
)

// LogPath is agent-proxy.log beside the credentials file.
func LogPath() string {
	return filepath.Join(filepath.Dir(settings.CredentialsPath()), "agent-proxy.log")
}

// Main runs agent-proxy in the foreground: `agent proxy` or the agent-proxy binary.
// `agent proxy stop` asks the running one to exit.
func Main(name string, args []string) int {
	if len(args) > 0 && args[0] == "stop" {
		return stopCommand(name)
	}
	flags := flag.NewFlagSet(name, flag.ContinueOnError)
	flags.Usage = func() {
		fmt.Fprintf(flags.Output(), "Usage: %s [--server URL] [--port N] [--intercept-port N]\n       %s stop\n\n"+
			"The proxy on this agent machine. Agents started with `agent` register with it, and it carries\n"+
			"their traffic to the agent-server this machine signed in to with `agent login <server>`.\n"+
			"stop asks the one running here to exit (agent starts it again when needed).\n\n", name, name)
		flags.PrintDefaults()
	}
	server := flags.String("server", "", "The agent-server to use (default: AI_PROXY_URL, then the one from `agent login`, then "+settings.DefaultServer+")")
	port := flags.Int("port", settings.ProxyPort(), "HTTP port on 127.0.0.1")
	interceptPort := flags.Int("intercept-port", settings.InterceptPort(), "HTTPS proxy port on 127.0.0.1 for account agents")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 2
	}
	if problem := checkPorts(*port, *interceptPort); problem != "" {
		fmt.Fprintln(os.Stderr, problem)
		return 1
	}
	logger := openLog()
	upstream := settings.Upstream{Fixed: *server}
	httpListener, err := Listen(*port)
	if err != nil {
		fmt.Fprintf(os.Stderr, "agent-proxy: %v\n", err)
		return 1
	}
	tunnelListener, err := Listen(*interceptPort)
	if err != nil {
		fmt.Fprintf(os.Stderr, "agent-proxy: %v\n", err)
		return 1
	}
	actual := tunnelListener.Addr().(*net.TCPAddr).Port
	proxy := New(upstream, actual, logger)
	fmt.Fprintf(os.Stderr, "agent-proxy: 127.0.0.1:%d, HTTPS proxy 127.0.0.1:%d, carrying traffic to %s\n",
		httpListener.Addr().(*net.TCPAddr).Port, actual, upstream.Server())
	logger.Printf("agent-proxy carrying traffic to %s; HTTPS proxy on 127.0.0.1:%d", upstream.Server(), actual)
	go func() { _ = proxy.ServeTunnels(tunnelListener) }()
	httpServer := &http.Server{Handler: proxy, ReadHeaderTimeout: 30 * time.Second, ErrorLog: logger}
	proxy.Shutdown = func() {
		logger.Printf("agent-proxy stopping, as asked from this computer")
		// Lets the answer out first; agents' streams in flight end with it.
		time.Sleep(100 * time.Millisecond)
		_ = httpServer.Close()
		_ = tunnelListener.Close()
	}
	if err := httpServer.Serve(httpListener); err != nil && !errors.Is(err, http.ErrServerClosed) {
		fmt.Fprintf(os.Stderr, "agent-proxy: %v\n", err)
		return 1
	}
	return 0
}

func stopCommand(name string) int {
	stopped, err := Stop(settings.ProxyURL())
	switch {
	case err != nil:
		fmt.Fprintf(os.Stderr, "%s stop: %v\n", name, err)
		return 1
	case !stopped:
		fmt.Println("agent-proxy isn't running.")
	default:
		fmt.Println("agent-proxy stopped; `agent` starts it again when needed.")
	}
	return 0
}

// Stop asks the agent-proxy at base to exit and waits until it has; false when none runs there.
func Stop(base string) (bool, error) {
	running := api.Health(base)
	if running == nil {
		return false, nil
	}
	if running["role"] != "agent-proxy" {
		return false, fmt.Errorf("something other than agent-proxy answers on %s", base)
	}
	response, err := settings.Client(5*time.Second).Post(base+"/shutdown", "application/json", nil)
	if err != nil {
		return false, err
	}
	response.Body.Close()
	if response.StatusCode != http.StatusOK {
		// An agent-proxy from before /shutdown: it has to be stopped by hand.
		return false, fmt.Errorf("the agent-proxy on %s (version %v) can't be stopped this way; end its process", base, running["version"])
	}
	for deadline := time.Now().Add(10 * time.Second); time.Now().Before(deadline); time.Sleep(100 * time.Millisecond) {
		if api.Health(base) == nil {
			return true, nil
		}
	}
	return false, fmt.Errorf("agent-proxy on %s is still answering", base)
}

func openLog() *log.Logger {
	path := LogPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err == nil {
		if file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600); err == nil {
			return log.New(file, "", log.LstdFlags)
		}
	}
	return log.New(io.Discard, "", 0)
}

// checkPorts gives a plain message when the ports are taken. Usually that's the agent-proxy
// `agent` started in the background.
func checkPorts(port, interceptPort int) string {
	busy := 0
	for _, candidate := range []int{port, interceptPort} {
		if candidate == 0 {
			continue
		}
		listener, err := Listen(candidate)
		if err != nil {
			busy = candidate
			break
		}
		listener.Close()
	}
	if busy == 0 {
		return ""
	}
	if running := api.Health("http://127.0.0.1:" + strconv.Itoa(port)); running != nil && running["role"] == "agent-proxy" {
		return fmt.Sprintf("agent-proxy is already running on 127.0.0.1:%d, carrying traffic to %v. "+
			"`agent` starts it in the background when needed; stop that process to run agent-proxy in the foreground.", port, running["server"])
	}
	return fmt.Sprintf("127.0.0.1:%d is in use by another program. Choose free ports with --port and --intercept-port "+
		"(or AI_PROXY_LOCAL_PORT and AI_PROXY_LOCAL_INTERCEPT_PORT).", busy)
}
