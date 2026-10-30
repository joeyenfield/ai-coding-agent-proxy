// Package command is agent-machine's command line: signing in, running in the fleet
// (in the foreground, or started in the background), and the machine's settings.
package command

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"ai-coding-agent-proxy/machine/internal/containers"
	"ai-coding-agent-proxy/machine/internal/login"
	"ai-coding-agent-proxy/machine/internal/mcp"
	"ai-coding-agent-proxy/machine/internal/presence"
	"ai-coding-agent-proxy/machine/internal/process"
	"ai-coding-agent-proxy/machine/internal/servers"
	"ai-coding-agent-proxy/machine/internal/settings"
)

const usage = `agent-machine puts this computer in an agent-server's fleet, so the server can use the
LLM providers it runs (Ollama, Strata, Docker Model Runner, containers) and start agents on it.

Usage: agent-machine                           this machine as the fleet sees it
       agent-machine login [url]                 (OIDC device sign-in)
       agent-machine logout [url]
       agent-machine start                     join the fleet in the background
       agent-machine stop
       agent-machine run [--server url]        join the fleet in the foreground (Ctrl+C leaves)
       agent-machine containers on|off         let the server run Strata or Ollama here in containers (off by default)
       agent-machine containers cli auto|docker|podman  which runs them (auto: whichever is installed and answering)
       agent-machine servers on|off            let the server manage the model servers here: start and stop them, their settings and models (off by default)
       agent-machine launches on|off           let the server start agents here (off by default)
       agent-machine commands on|off           let you open a terminal here from the console, in a launch folder (off by default)
       agent-machine roots add <dir>           a folder agents may be started in (and any folder inside it)
       agent-machine roots remove <dir>
       agent-machine mcp                       the MCP servers this machine shares with the server's agents
       agent-machine mcp add <name> [--env NAME=VALUE]... [--dir <dir>] [--description <text>] -- <command> [args...]
       agent-machine mcp add <name> --url <url> [--header "Name: value"]... [--description <text>]
       agent-machine mcp remove <name>
       agent-machine name <name>|-             the name the console shows (default: the hostname)
       agent-machine address <host>|-          how the server reaches this machine's model servers
       agent-machine update [--force]          replace agent-machine with the build the server offers, and restart it
       agent-machine updates on|off            let the console update agent-machine and agent here to the server's build (off by default)
       agent-machine version

Ollama, Strata and Docker Model Runner running here are found by themselves, and the console
sees their models, logs and this machine's load through agent-machine. With servers on, it can
also pull, delete, load and unload models, start Ollama (ollama serve, with the settings saved
from the console) and Strata's saved profiles here, and stop them.
Containers run Strata or Ollama images only, with their own settings; docker run is root here.
Launches run the agent command (agent) from PATH, signed in to the same server with agent login.
Only you, signed in as this machine's owner, can start agents, containers or terminals here.
A terminal from the console (commands on) is a shell as this user: only you see it.
MCP servers run here, as this user, for the agents you choose and the teams you let use them
(the machine's Access tab). Their commands, environments and URLs stay here.
Settings changes reach the server within a minute.
`

// Main runs agent-machine with its arguments and returns the exit status.
func Main(args []string) int {
	if len(args) == 0 {
		show(settings.LoadMachine())
		return 0
	}
	switch args[0] {
	case "-h", "--help", "help":
		fmt.Print(usage)
		return 0
	case "version", "--version":
		fmt.Printf("agent-machine %s (protocol %d)\n", settings.Version, settings.Protocol)
		return 0
	case "update":
		return updateCommand(args[1:])
	case "login":
		return login.Login(args[1:])
	case "logout":
		return login.Logout(args[1:])
	case "run":
		return run(args[1:])
	case "start":
		return start(args[1:])
	case "stop":
		return stop()
	case "status":
		show(settings.LoadMachine())
		return 0
	case "mcp":
		return mcpCommand(args[1:])
	}
	return configure(args)
}

func fail(message string) int {
	fmt.Fprintf(os.Stderr, "agent-machine: %s\n\n%s", message, usage)
	return 2
}

// configure changes one of machine.json's settings, then shows the machine.
func configure(args []string) int {
	machine := settings.LoadMachine()
	switch args[0] {
	case "updates":
		if len(args) != 2 || (args[1] != "on" && args[1] != "off") {
			return fail("updates takes on or off")
		}
		machine.Updates = args[1] == "on"
		if machine.Updates {
			fmt.Println("Updates are on: the machine's owner or an admin can update agent-machine and agent here to the server's build from the console.")
		}
	case "commands":
		if len(args) != 2 || (args[1] != "on" && args[1] != "off") {
			return fail("commands takes on or off")
		}
		machine.Commands = args[1] == "on"
		if machine.Commands {
			fmt.Println("Commands are on: signed in as this machine's owner, you can open a terminal here from the console, as this user.")
			if len(machine.Roots) == 0 {
				fmt.Println("Terminals start in a launch folder, and there's none yet: agent-machine roots add <dir>")
			}
		}
	case "launches":
		if len(args) != 2 || (args[1] != "on" && args[1] != "off") {
			return fail("launches takes on or off")
		}
		machine.Launches = args[1] == "on"
		if machine.Launches && len(machine.Roots) == 0 {
			fmt.Println("Launches are on, but there's no folder to start agents in yet: agent-machine roots add <dir>")
		}
		if _, err := presence.AgentCommand(); machine.Launches && err != nil {
			fmt.Println("Note:", err)
		}
	case "roots":
		if len(args) != 3 || (args[1] != "add" && args[1] != "remove") {
			return fail("roots takes add or remove, and a folder")
		}
		dir, err := filepath.Abs(args[2])
		if err != nil {
			return fail(err.Error())
		}
		if args[1] == "add" {
			if info, err := os.Stat(dir); err != nil || !info.IsDir() {
				return fail(dir + " isn't a folder")
			}
			if !contains(machine.Roots, dir) {
				machine.Roots = append(machine.Roots, dir)
			}
		} else {
			kept := machine.Roots[:0]
			for _, root := range machine.Roots {
				if !strings.EqualFold(filepath.Clean(root), filepath.Clean(dir)) {
					kept = append(kept, root)
				}
			}
			machine.Roots = kept
		}
	case "containers":
		switch {
		case len(args) == 2 && (args[1] == "on" || args[1] == "off"):
			machine.Containers = args[1] == "on"
		case len(args) == 3 && args[1] == "cli" && (args[2] == "auto" || args[2] == "docker" || args[2] == "podman"):
			machine.ContainerCLI = args[2]
			if args[2] == "auto" {
				machine.ContainerCLI = ""
			}
		default:
			return fail("containers takes on, off, or cli auto|docker|podman")
		}
	case "servers":
		if len(args) >= 2 && args[1] == "service" {
			// sudo agent-machine servers service [off]: let this user restart Ollama's systemd service.
			off := len(args) == 3 && args[2] == "off"
			if err := servers.SetUpService(os.Getenv("SUDO_USER"), off); err != nil {
				return fail(err.Error())
			}
			if off {
				fmt.Println("agent-machine can no longer restart ollama.service or change its settings.")
			} else {
				fmt.Printf("%s's agent-machine may now start, stop and restart ollama.service, and set its OLLAMA_* settings (%s).\n", os.Getenv("SUDO_USER"), servers.ServiceEnvPath)
				fmt.Println("Undo it with: sudo agent-machine servers service off")
			}
			return 0
		}
		if len(args) != 2 || (args[1] != "on" && args[1] != "off") {
			return fail("servers takes on, off, or service [off] (with sudo)")
		}
		machine.Servers = args[1] == "on"
	case "name", "address":
		if len(args) != 2 {
			return fail(args[0] + " takes one value, or - to clear it")
		}
		value := args[1]
		if value == "-" {
			value = ""
		}
		if args[0] == "name" {
			machine.Name = value
		} else {
			machine.Address = value
		}
	default:
		return fail("unknown command " + args[0])
	}
	if err := machine.Save(); err != nil {
		fmt.Fprintf(os.Stderr, "agent-machine: %v\n", err)
		return 1
	}
	show(machine)
	return 0
}

// running is the agent-machine already in the fleet, if one is.
func running() *settings.Status {
	status := settings.LoadStatus()
	if status.PID == 0 || status.PID == os.Getpid() || !process.Alive(status.PID) {
		return nil
	}
	return status
}

// run stays in the fleet until interrupted, reconnecting whenever the connection drops.
func run(args []string) int {
	flags := flag.NewFlagSet("agent-machine run", flag.ContinueOnError)
	server := flags.String("server", "", "The agent-server to join (default: the one from agent-machine login, then "+settings.DefaultServer+")")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 2
	}
	if other := running(); other != nil {
		fmt.Fprintf(os.Stderr, "agent-machine is already running (process %d, %s). Stop it first: agent-machine stop\n", other.PID, other.State)
		return 1
	}
	logger := openLog()
	upstream := settings.Upstream{Fixed: *server}
	status := &settings.Status{PID: os.Getpid(), Server: upstream.Server(), State: "connecting", Started: startedBy()}
	_ = status.Save()
	logger.Printf("agent-machine %s joining %s", settings.Version, status.Server)
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	member := &presence.Presence{
		Upstream: upstream, Logger: logger,
		OnStatus: func(state string) {
			status.State = state
			_ = status.Save()
		},
		OnServerVersion: func(version string) {
			status.ServerVersion = version
			_ = status.Save()
		},
	}
	member.Run(ctx)
	status.PID, status.State = 0, "stopped"
	_ = status.Save()
	logger.Printf("agent-machine left the fleet")
	return 0
}

// openLog writes to agent-machine.log, and to the terminal too when there is one.
func openLog() *log.Logger {
	var writers []io.Writer
	if err := os.MkdirAll(settings.Home(), 0o700); err == nil {
		if file, err := os.OpenFile(settings.LogPath(), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600); err == nil {
			writers = append(writers, file)
		}
	}
	if info, err := os.Stderr.Stat(); err == nil && info.Mode()&os.ModeCharDevice != 0 {
		writers = append(writers, os.Stderr)
	}
	return log.New(io.MultiWriter(writers...), "", log.LstdFlags)
}

// startedEnv tells `agent-machine run` that agent-machine start ran it, in the background.
const startedEnv = "AI_PROXY_MACHINE_STARTED"

// startedBy is how this run began, for status.json: update restarts a background one
// itself, leaves a service's to its service manager, and one in a terminal to its user.
func startedBy() string {
	if os.Getenv(startedEnv) == "background" {
		return "background"
	}
	if info, err := os.Stdin.Stat(); err == nil && info.Mode()&os.ModeCharDevice != 0 {
		return "terminal"
	}
	return "service"
}

// start runs `agent-machine run` detached from this terminal, and waits until it has connected or failed to.
func start(args []string) int {
	self, err := os.Executable()
	if err != nil {
		fmt.Fprintf(os.Stderr, "agent-machine: %v\n", err)
		return 1
	}
	return startWith(self, args)
}

// startWith is start with a given binary: the new one, after an update.
func startWith(binary string, args []string) int {
	if other := running(); other != nil {
		fmt.Printf("agent-machine is already running (process %d): %s\n", other.PID, other.State)
		return 0
	}
	cmd := exec.Command(binary, append([]string{"run"}, args...)...)
	cmd.Env = append(os.Environ(), startedEnv+"=background")
	cmd.Stdin, cmd.Stdout, cmd.Stderr = nil, nil, nil
	process.Detach(cmd)
	if err := cmd.Start(); err != nil {
		fmt.Fprintf(os.Stderr, "agent-machine: couldn't start: %v\n", err)
		return 1
	}
	pid := cmd.Process.Pid
	_ = cmd.Process.Release()
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		status := settings.LoadStatus()
		if status.PID == pid && status.State != "connecting" {
			fmt.Printf("agent-machine started (process %d): %s\nIts log: %s\n", pid, status.State, settings.LogPath())
			return 0
		}
		if !process.Alive(pid) {
			fmt.Fprintf(os.Stderr, "agent-machine exited while starting. Run `agent-machine run` to see why (log: %s)\n", settings.LogPath())
			return 1
		}
		time.Sleep(200 * time.Millisecond)
	}
	fmt.Printf("agent-machine started (process %d), still connecting. Its log: %s\n", pid, settings.LogPath())
	return 0
}

func stop() int {
	other := running()
	if other == nil {
		fmt.Println("agent-machine isn't running.")
		return 0
	}
	if err := process.Stop(other.PID); err != nil {
		fmt.Fprintf(os.Stderr, "agent-machine: couldn't stop process %d: %v\n", other.PID, err)
		return 1
	}
	for deadline := time.Now().Add(10 * time.Second); time.Now().Before(deadline) && process.Alive(other.PID); {
		time.Sleep(100 * time.Millisecond)
	}
	if process.Alive(other.PID) {
		fmt.Fprintf(os.Stderr, "agent-machine (process %d) is still running\n", other.PID)
		return 1
	}
	// A killed process (Windows has no SIGTERM) can't say it stopped; say it for it.
	status := settings.LoadStatus()
	if status.PID == other.PID {
		status.PID, status.State = 0, "stopped"
		_ = status.Save()
	}
	fmt.Println("agent-machine stopped; this machine has left the fleet.")
	return 0
}

func show(machine *settings.Machine) {
	upstream := settings.Upstream{}
	server := upstream.Server()
	signedIn := "not signed in (agent-machine login " + server + ")"
	if entry := settings.LoadCredentials().Servers[settings.Normalize(server)]; entry != nil {
		switch {
		case entry.AccessToken != "" && entry.User != "":
			signedIn = "as " + entry.User
		case entry.AccessToken != "":
			signedIn = "with OAuth"
		default:
			signedIn = "OAuth sign-in required (agent-machine login " + server + ")"
		}
	}
	if os.Getenv(settings.TokenEnvironment) != "" {
		signedIn = "with an injected OAuth access token"
	}
	fmt.Printf("Machine   : %s (%s)\nServer    : %s, %s\n", machine.DisplayName(), machine.ID, server, signedIn)
	if status := running(); status != nil {
		fmt.Printf("Fleet     : %s (process %d)\n", status.State, status.PID)
		if notice := presence.VersionNotice(settings.Version, status.ServerVersion); notice != "" {
			fmt.Println("Version   :", notice)
		}
	} else {
		fmt.Println("Fleet     : not running (agent-machine start)")
	}
	if machine.Address != "" {
		fmt.Println("Address   :", machine.Address)
	}
	providers := presence.DetectProviders()
	if len(providers) == 0 {
		fmt.Println("Providers : no Ollama, Strata or Docker Model Runner answering here")
	}
	for index, provider := range providers {
		label := "Providers :"
		if index > 0 {
			label = "           "
		}
		fmt.Printf("%s %s on port %d\n", label, provider.Type, provider.Port)
	}
	cli := containers.CLIFor(machine)
	how := " (found by itself; agent-machine containers cli docker|podman picks one)"
	if machine.ContainerCLI != "" {
		how = " (chosen; agent-machine containers cli auto finds it by itself)"
	}
	switch {
	case !machine.Containers:
		fmt.Println("Containers: off (agent-machine containers on)")
	case cli.Command() == nil && machine.ContainerCLI == "":
		fmt.Println("Containers: on, but neither docker nor podman is installed or on PATH")
	case cli.Command() == nil:
		fmt.Println("Containers: on, but", cli.Name, "isn't installed or isn't on PATH")
	case !cli.Probe().Online:
		fmt.Println("Containers: on, with " + cli.Name + how + ", but its engine isn't answering (start Docker Desktop, or `podman machine start`)")
	default:
		fmt.Println("Containers: on, with " + cli.Name + how)
	}
	if machine.Servers {
		fmt.Println("Servers   : on: the server can manage the model servers here (start, stop, settings, models)")
	} else {
		fmt.Println("Servers   : off (agent-machine servers on)")
	}
	launches := "off (agent-machine launches on)"
	if machine.Launches {
		launches = "on"
		if _, err := presence.AgentCommand(); err != nil {
			launches += ", but " + err.Error()
		}
	}
	fmt.Println("Launches  :", launches)
	if machine.Updates {
		fmt.Println("Updates   : on: the console can update agent-machine and agent here to the server's build")
	} else {
		fmt.Println("Updates   : off (agent-machine updates on)")
	}
	if machine.Commands {
		fmt.Println("Commands  : on: you can open a terminal here from the console")
	} else {
		fmt.Println("Commands  : off (agent-machine commands on)")
	}
	if machine.Launches && len(machine.Roots) == 0 {
		fmt.Println("Folders   : none (agent-machine roots add <dir>)")
	}
	if servers, err := mcp.Load(); err == nil && len(servers) > 0 {
		for index, server := range servers {
			label := "MCP       :"
			if index > 0 {
				label = "           "
			}
			fmt.Println(label, describeMCP(server))
		}
	}
	for index, root := range machine.Roots {
		label := "Folders   :"
		if index > 0 {
			label = "           "
		}
		fmt.Println(label, root)
	}
}

func contains(values []string, value string) bool {
	for _, item := range values {
		if strings.EqualFold(filepath.Clean(item), filepath.Clean(value)) {
			return true
		}
	}
	return false
}
