package command

import (
	"errors"
	"flag"
	"fmt"
	"os"
	"os/exec"

	"ai-coding-agent-proxy/machine/internal/presence"
	"ai-coding-agent-proxy/machine/internal/process"
	"ai-coding-agent-proxy/machine/internal/settings"
	"ai-coding-agent-proxy/machine/internal/update"
)

// Programs is agent-machine's binary, for update.
var Programs = []string{"agent-machine"}

// updateCommand is `agent-machine update [--force] [--server URL]`: the server's build for
// this computer in place of this binary, then the running agent-machine restarted on it.
func updateCommand(args []string) int {
	flags := flag.NewFlagSet("agent-machine update", flag.ContinueOnError)
	flags.Usage = func() {
		fmt.Fprint(flags.Output(), "Usage: agent-machine update [--force] [--server URL]\n\n"+
			"Replaces agent-machine here with the build the agent-server offers for this computer\n"+
			"("+settings.Platform()+"), checked against the server's SHA-256, and restarts the running one.\n\n")
		flags.PrintDefaults()
	}
	force := flags.Bool("force", false, "Replace it even when the server's version is this one")
	server := flags.String("server", "", "The agent-server to update from (default: the one from agent-machine login)")
	withAgent := flags.Bool("with-agent", false, "Update the agent command first (agent update), as a remote update from the console asks")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 2
	}
	from := *server
	if from == "" {
		from = (settings.Upstream{}).Server()
		if status := running(); status != nil && status.Server != "" {
			from = status.Server
		}
	}
	if *withAgent {
		updateAgent(from)
	}
	index, err := update.Fetch(from)
	if err != nil {
		fmt.Fprintf(os.Stderr, "agent-machine update: %v\n", err)
		return 1
	}
	if index.Version == settings.Version && !*force {
		fmt.Printf("agent-machine %s is the version the agent-server at %s offers; nothing to do (--force replaces it anyway).\n", settings.Version, from)
		return 0
	}
	dir, err := update.Location()
	if err != nil {
		fmt.Fprintf(os.Stderr, "agent-machine update: %v\n", err)
		return 1
	}
	steps, err := update.Plan(index, settings.Platform(), dir, Programs)
	if err == nil {
		err = update.Apply(from, steps)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "agent-machine update: %v\n", err)
		return 1
	}
	fmt.Printf("Updated agent-machine in %s: %s -> %s.\n", dir, settings.Version, index.Version)
	if !index.Exact {
		fmt.Printf("Note: the server runs %s; this is its closest build (%s).\n", index.ServerVersion, index.Version)
	}
	return restartAfterUpdate(steps[0].Target)
}

// updateAgent runs `agent update` from the same server, first: launches and terminals run the agent
// command, so it should be the server's build too. Its own failure doesn't stop agent-machine's update.
func updateAgent(server string) {
	program, err := presence.AgentCommand()
	if err != nil {
		fmt.Println("Not updating agent:", err)
		return
	}
	cmd := exec.Command(program, "update", "--server", server)
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	process.Quiet(cmd)
	if err := cmd.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "agent update: %v\n", err)
	}
}

// restartAfterUpdate puts the running agent-machine on the new binary, however it was started.
func restartAfterUpdate(binary string) int {
	other := running()
	if other == nil {
		return 0
	}
	switch other.Started {
	case "terminal":
		fmt.Printf("agent-machine runs in a terminal (process %d): stop it there and run it again for the new version.\n", other.PID)
		return 0
	case "service":
		// systemd (Restart=always) and launchd (KeepAlive) start it again, now from the new file.
		if code := stop(); code != 0 {
			return code
		}
		fmt.Println("Its service starts it again with the new version within a few seconds (systemd, launchd); if yours doesn't, restart the service.")
		return 0
	}
	if code := stop(); code != 0 {
		return code
	}
	var args []string
	if other.Server != "" {
		args = []string{"--server", other.Server}
	}
	return startWith(binary, args)
}
