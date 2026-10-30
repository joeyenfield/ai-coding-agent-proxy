package launcher

import (
	"fmt"
	"strings"
)

// Options are the launcher's own arguments; Extra goes to the agent.
type Options struct {
	Agent   string
	Project string
	Model   string
	Backend string
	// MCP names shared MCP servers for the agent: "<machine>/<name>", a machine's name for all of
	// its servers, or "all" for every one this user may use.
	MCP           []string
	Trace         bool
	List          bool
	Control       bool
	Switch        bool
	Share         string
	Team          string
	LetOthersType bool
	Help          bool
	// Headless runs the agent with no local window, its terminal shared with the server: a
	// launch from the console, run by agent-machine (the machine/ component).
	Headless bool
	LaunchID string
	// Shell runs the user's shell instead of an agent, in a terminal shared with the server only
	// for its owner: `agent --shell [folder]`, and agent-machine's terminals from the console.
	Shell bool
	Extra []string
	// Proxy is the old --proxy flag, refused with a pointer to `agent login`.
	Proxy string
}

// Usage is `agent --help`.
const Usage = `Usage: agent <agent> [project] [options] [-- agent arguments]
       agent --list
       agent --shell [folder]             share a shell in a private terminal, to use it from the console
       agent login [url]                         (OIDC device sign-in)
                                          sign this machine in to an agent-server
       agent logout [url]
       agent proxy [--server url]         run this machine's agent-proxy in the foreground
       agent proxy stop                   stop the agent-proxy running here (agent starts it again)
       agent update [--force]             replace agent and agent-proxy with the build the server offers
       agent version

Launch a coding agent through this machine's agent-proxy. To put this machine in the
server's fleet (its LLM providers, containers, launches from the console), use agent-machine.

Options:
  -m, --model NAME        Model name or alias from the server's models.yaml (optional for account agents)
      --backend NAME      One of the server's backends (default: the one the server picks for the agent)
      --trace             Capture full request and response payloads
      --mcp NAMES         MCP servers machines share, for the agent: machine/name, a machine for
                          all of its servers, or all; comma-separated or repeated
      --list              List the server's agents and whether they're installed here
      --control           Share the agent's terminal with the server, to watch and type into it from
                          the console or a phone (or set AI_PROXY_CONTROL=1)
      --switch            If the backend is a container a machine runs, stop other containers using
                          its GPU to start it

Sharing:
      --share WHO         private, team or org: who else can see this session (default: private)
      --team NAME         Which of your teams (org/team) with --share team, or which of your
                          organizations with --share org (needed if you're in several)
      --let-others-type   With --control: people it's shared with can type into its terminal and end
                          it, not only watch

Unknown options, and everything after --, are passed to the agent.
`

// valued are the options that take a value, as --name value or --name=value.
var valued = map[string]bool{"--model": true, "-m": true, "--backend": true, "--share": true, "--team": true, "--proxy": true, "--launch-id": true, "--mcp": true}

// ParseArgs reads arguments the way argparse's parse_known_args did: options anywhere,
// the first two plain arguments as the agent and project, everything else for the agent.
func ParseArgs(args []string) (Options, error) {
	options := Options{Project: ".", Share: "private"}
	positional := 0
	for index := 0; index < len(args); index++ {
		arg := args[index]
		if arg == "--" {
			options.Extra = append(options.Extra, args[index+1:]...)
			break
		}
		name, value, hasValue := strings.Cut(arg, "=")
		if !strings.HasPrefix(arg, "-") || arg == "-" {
			switch positional {
			case 0:
				options.Agent = arg
			case 1:
				options.Project = arg
			default:
				options.Extra = append(options.Extra, arg)
			}
			positional++
			continue
		}
		if valued[name] {
			if !hasValue {
				if index+1 >= len(args) {
					return options, fmt.Errorf("%s needs a value", name)
				}
				index++
				value = args[index]
			}
			switch name {
			case "--model", "-m":
				options.Model = value
			case "--backend":
				options.Backend = value
			case "--share":
				if value != "private" && value != "team" && value != "org" {
					return options, fmt.Errorf("--share must be private, team or org")
				}
				options.Share = value
			case "--team":
				options.Team = value
			case "--proxy":
				options.Proxy = value
			case "--launch-id":
				options.LaunchID = value
			case "--mcp":
				for _, name := range strings.Split(value, ",") {
					if name = strings.TrimSpace(name); name != "" {
						options.MCP = append(options.MCP, name)
					}
				}
			}
			continue
		}
		switch arg {
		case "--trace":
			options.Trace = true
		case "--list":
			options.List = true
		case "--control":
			options.Control = true
		case "--switch":
			options.Switch = true
		case "--let-others-type":
			options.LetOthersType = true
		case "--headless":
			options.Headless = true
		case "--shell":
			options.Shell = true
		case "-h", "--help":
			options.Help = true
		case "--no-remote":
			// Old flag, kept so it isn't passed on to the agent: sharing is off unless --control.
		default:
			options.Extra = append(options.Extra, arg)
		}
	}
	if options.Shell {
		// `agent --shell [folder]`: the one plain argument is the folder, not an agent.
		if positional > 1 {
			return options, fmt.Errorf("--shell takes one folder")
		}
		if positional == 1 {
			options.Project = options.Agent
		}
		options.Agent = ""
	}
	return options, nil
}

// Sharing is the session's sharing for the server.
func (o Options) Sharing() map[string]any {
	sharing := map[string]any{"visibility": o.Share, "access": "view"}
	if o.Team != "" {
		sharing["team"] = o.Team
	}
	if o.LetOthersType {
		sharing["access"] = "control"
	}
	return sharing
}
