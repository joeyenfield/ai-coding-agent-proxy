// agent is the command people run on an agent machine: it launches coding agents through
// this machine's agent-proxy, signs the machine in to an agent-server, and runs the
// agent-proxy itself (`agent proxy`), so one binary is all an agent machine needs.
package main

import (
	"fmt"
	"os"

	"ai-control-mesh/client/internal/accounts"
	"ai-control-mesh/client/internal/launcher"
	"ai-control-mesh/client/internal/localproxy"
	"ai-control-mesh/client/internal/login"
	"ai-control-mesh/client/internal/settings"
	"ai-control-mesh/client/internal/update"
)

// version is set at build time: go build -ldflags "-X main.version=1.2.3".
var version = "dev"

func main() {
	settings.Version = version
	// What an earlier `agent update` left behind, once it's no longer running.
	if dir, err := update.Location(); err == nil {
		update.Cleanup(dir, update.Programs)
	}
	args := os.Args[1:]
	if len(args) > 0 {
		switch args[0] {
		case "login":
			os.Exit(login.Login(args[1:]))
		case "logout":
			os.Exit(login.Logout(args[1:]))
		case "machine":
			// The fleet moved to its own command, so a machine can supply models without running agents.
			fmt.Fprintln(os.Stderr, "Machines join the fleet with their own command now: agent-machine (agent-machine --help).")
			os.Exit(2)
		case "accounts":
			os.Exit(accounts.Main(args[1:]))
		case "proxy":
			os.Exit(localproxy.Main("agent proxy", args[1:]))
		case "update":
			os.Exit(update.Main(args[1:]))
		case "version", "--version":
			fmt.Printf("agent %s (protocol %d)\n", version, settings.Protocol)
			return
		}
	}
	os.Exit(launcher.Main(args))
}
