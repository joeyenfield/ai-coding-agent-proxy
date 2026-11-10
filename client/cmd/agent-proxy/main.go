// agent-proxy runs this machine's agent-proxy in the foreground: the same as `agent proxy`,
// for people who'd rather run it as a service of its own.
package main

import (
	"os"

	"ai-control-mesh/client/internal/localproxy"
	"ai-control-mesh/client/internal/settings"
	"ai-control-mesh/client/internal/update"
)

// version is set at build time: go build -ldflags "-X main.version=1.2.3".
var version = "dev"

func main() {
	settings.Version, settings.Program = version, "agent-proxy"
	if dir, err := update.Location(); err == nil {
		update.Cleanup(dir, update.Programs)
	}
	os.Exit(localproxy.Main("agent-proxy", os.Args[1:]))
}
