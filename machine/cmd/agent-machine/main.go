// agent-machine is the command for a machine in an agent-server's fleet: it signs the
// machine in (agent-machine login), keeps it connected to the server, and through that
// connection supplies the LLM providers it runs, runs the containers the server asks for
// and starts agents there, once the machine allows each. See ../../README.md.
package main

import (
	"os"

	"ai-coding-agent-proxy/machine/internal/command"
	"ai-coding-agent-proxy/machine/internal/settings"
	"ai-coding-agent-proxy/machine/internal/update"
)

// version is set at build time: go build -ldflags "-X main.version=1.2.3".
var version = "dev"

func main() {
	settings.Version = version
	// What an earlier `agent-machine update` left behind, once it no longer runs.
	if dir, err := update.Location(); err == nil {
		update.Cleanup(dir, command.Programs)
	}
	os.Exit(command.Main(os.Args[1:]))
}
