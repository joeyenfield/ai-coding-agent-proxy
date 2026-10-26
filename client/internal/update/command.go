package update

import (
	"errors"
	"flag"
	"fmt"
	"os"

	"ai-coding-agent-proxy/client/internal/api"
	"ai-coding-agent-proxy/client/internal/localproxy"
	"ai-coding-agent-proxy/client/internal/settings"
)

// Programs are the client's binaries, replaced together: agent and agent-proxy.
var Programs = []string{"agent", "agent-proxy"}

// Main is `agent update [--force] [--server URL]`.
func Main(args []string) int {
	flags := flag.NewFlagSet("agent update", flag.ContinueOnError)
	flags.Usage = func() {
		fmt.Fprint(flags.Output(), "Usage: agent update [--force] [--server URL]\n\n"+
			"Replaces agent and agent-proxy here with the build the agent-server offers for this computer\n"+
			"("+settings.Platform()+"), checked against the server's SHA-256, then stops the running agent-proxy\n"+
			"so the next agent starts the new one.\n\n")
		flags.PrintDefaults()
	}
	force := flags.Bool("force", false, "Replace them even when the server's version is this one")
	server := flags.String("server", "", "The agent-server to update from (default: the one this machine's agent-proxy uses)")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 2
	}
	from := *server
	if from == "" {
		from = settings.DefaultServerURL()
		if running := api.Health(settings.ProxyURL()); running != nil && running["role"] == "agent-proxy" {
			if value, ok := running["server"].(string); ok && value != "" {
				from = value
			}
		}
	}
	index, err := Fetch(from)
	if err != nil {
		fmt.Fprintf(os.Stderr, "agent update: %v\n", err)
		return 1
	}
	if index.Version == settings.Version && !*force {
		fmt.Printf("agent %s is the version the agent-server at %s offers; nothing to do (--force replaces it anyway).\n", settings.Version, from)
		return 0
	}
	dir, err := Location()
	if err != nil {
		fmt.Fprintf(os.Stderr, "agent update: %v\n", err)
		return 1
	}
	steps, err := Plan(index, settings.Platform(), dir, Programs)
	if err == nil {
		err = Apply(from, steps)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "agent update: %v\n", err)
		return 1
	}
	fmt.Printf("Updated agent and agent-proxy in %s: %s -> %s.\n", dir, settings.Version, index.Version)
	if !index.Exact {
		fmt.Printf("Note: the server runs %s; this is its closest build (%s).\n", index.ServerVersion, index.Version)
	}
	// The running agent-proxy is the old binary: stop it, and the next agent starts the new one.
	stopped, err := localproxy.Stop(settings.ProxyURL())
	switch {
	case err != nil:
		fmt.Printf("Stop the running agent-proxy so the new one takes over: %v\n", err)
	case stopped:
		fmt.Println("Stopped the running agent-proxy; the next agent starts the new one.")
	}
	return 0
}
