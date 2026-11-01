package accounts

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"strings"

	"golang.org/x/term"

	"ai-coding-agent-proxy/client/internal/api"
	"ai-coding-agent-proxy/client/internal/launcher"
)

// Usage is `agent accounts --help`.
const Usage = `Usage: agent accounts list                 your provider accounts on the server, and whether each can run
       agent accounts detect [--json]     the provider CLIs here and whether each is signed in (no tokens are read)
       agent accounts status              both: what's here, and your accounts
       agent accounts enroll <claude|codex|copilot> [options]
                                          register this computer as a machine the subscription runs on

Your AI subscriptions (Claude, ChatGPT through Codex, GitHub Copilot), apart from your sign-in to
the server (agent login). Sign in with the provider's own CLI first; enrolling tells the server this
computer is signed in, and never sends the sign-in itself.

Enroll options:
  --name NAME       The account's name on the server (enrolling again with the same name finds it)
  --plan PLAN       Its plan (Pro, Max 5x, Plus, Pro+, ...), for you to tell accounts apart
  --machine ID      Which of your machines this is (default: the one with this computer's hostname)
  --force           Enroll even if the CLI doesn't look signed in here
  --token-stdin     Copilot only: store a fine-grained personal access token with the Copilot Requests
                    permission, read from standard input, encrypted on the server, for the machines you
                    assign the account to (instead of signing in on each)
  --yes             Don't ask before storing the token
`

// Account is one provider account, as GET /api/provider-accounts lists it (metadata only).
type Account struct {
	ID                 string `json:"id"`
	Provider           string `json:"provider"`
	DisplayName        string `json:"display_name"`
	PlanName           string `json:"plan_name"`
	Owner              string `json:"owner"`
	AuthenticationMode string `json:"authentication_mode"`
	Status             string `json:"status"`
	State              string `json:"state"`
	StateReason        string `json:"state_reason"`
	Credential         struct {
		Stored bool `json:"stored"`
	} `json:"credential"`
	Assignments []struct {
		MachineID string `json:"machine_id"`
		Health    struct {
			MachineName string `json:"machine_name"`
			Available   bool   `json:"available"`
		} `json:"health"`
	} `json:"assignments"`
}

// Main runs `agent accounts ...` and returns the exit status.
func Main(args []string) int {
	if len(args) == 0 || args[0] == "-h" || args[0] == "--help" {
		fmt.Print(Usage)
		if len(args) == 0 {
			return 2
		}
		return 0
	}
	var err error
	switch args[0] {
	case "detect":
		err = detectCommand(args[1:], os.Stdout)
	case "list":
		err = listCommand(os.Stdout)
	case "status":
		if err = detectCommand(nil, os.Stdout); err == nil {
			fmt.Println()
			err = listCommand(os.Stdout)
		}
	case "enroll":
		err = enrollCommand(args[1:])
	default:
		fmt.Fprintf(os.Stderr, "agent accounts: no command %s\n\n%s", args[0], Usage)
		return 2
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "agent accounts: %v\n", err)
		return 1
	}
	return 0
}

func detectCommand(args []string, out io.Writer) error {
	report := Detect(Local())
	if len(args) > 0 && args[0] == "--json" {
		encoded, _ := json.Marshal(report)
		fmt.Fprintln(out, string(encoded))
		return nil
	}
	fmt.Fprintln(out, "Provider CLIs on this computer:")
	for _, cli := range report.CLIs {
		state := "not installed"
		if cli.Installed {
			state = strings.ReplaceAll(cli.Auth, "_", " ")
			if cli.Method != "" {
				state += " (" + cli.Method + ")"
			}
		}
		version := cli.Version
		if version == "" {
			version = "-"
		}
		fmt.Fprintf(out, "  %-8s %-26s %-28s %s\n", cli.Provider, version, state, strings.Join(cli.Modes, ", "))
		if cli.Note != "" && cli.Installed {
			fmt.Fprintf(out, "           %s\n", cli.Note)
		}
	}
	return nil
}

func listCommand(out io.Writer) error {
	client, server, err := launcher.Connect()
	if err != nil {
		return err
	}
	var listed struct {
		Accounts []Account `json:"accounts"`
	}
	if err := client.Get("/api/provider-accounts", &listed); err != nil {
		return err
	}
	if len(listed.Accounts) == 0 {
		fmt.Fprintf(out, "No provider accounts on %s yet: agent accounts enroll <claude|codex|copilot>\n", server)
		return nil
	}
	fmt.Fprintf(out, "Provider accounts on %s:\n", server)
	for _, account := range listed.Accounts {
		machines := []string{}
		for _, item := range account.Assignments {
			name := item.Health.MachineName
			if name == "" {
				name = item.MachineID
			}
			if !item.Health.Available {
				name += " (not ready)"
			}
			machines = append(machines, name)
		}
		where := strings.Join(machines, ", ")
		if where == "" {
			where = "no machines yet"
		}
		owner := ""
		if account.Status == "disabled" {
			owner = " [turned off]"
		}
		fmt.Fprintf(out, "  %s  %s (%s, %s)%s\n      %s: %s\n      runs on: %s\n", account.ID, account.DisplayName, account.Provider, account.AuthenticationMode, owner, account.State, account.StateReason, where)
	}
	return nil
}

type enrollOptions struct {
	provider, name, plan, machine string
	force, tokenStdin, yes        bool
}

func parseEnroll(args []string) (enrollOptions, error) {
	options := enrollOptions{}
	for index := 0; index < len(args); index++ {
		arg := args[index]
		name, value, hasValue := strings.Cut(arg, "=")
		takes := map[string]*string{"--name": &options.name, "--plan": &options.plan, "--machine": &options.machine}
		if target, ok := takes[name]; ok {
			if !hasValue {
				if index+1 >= len(args) {
					return options, fmt.Errorf("%s needs a value", name)
				}
				index++
				value = args[index]
			}
			*target = value
			continue
		}
		switch arg {
		case "--force":
			options.force = true
		case "--token-stdin":
			options.tokenStdin = true
		case "--yes", "-y":
			options.yes = true
		default:
			if strings.HasPrefix(arg, "-") || options.provider != "" {
				return options, fmt.Errorf("unknown argument %s", arg)
			}
			options.provider = arg
		}
	}
	known := false
	for _, provider := range Providers {
		known = known || provider == options.provider
	}
	if !known {
		return options, fmt.Errorf("enroll which provider: %s", strings.Join(Providers, ", "))
	}
	if options.tokenStdin && options.provider != "copilot" {
		return options, errors.New("only Copilot takes a stored token: Claude Code and Codex stay signed in on each machine")
	}
	return options, nil
}

func enrollCommand(args []string) error {
	options, err := parseEnroll(args)
	if err != nil {
		return err
	}
	var cli CLI
	for _, item := range Detect(Local()).CLIs {
		if item.Provider == options.provider {
			cli = item
		}
	}
	if !options.tokenStdin && !options.force {
		switch {
		case !cli.Installed:
			return fmt.Errorf("%s isn't installed here; install it, sign in, then enroll (or --force)", options.provider)
		case cli.Auth == "signed_out" || cli.Subscription != nil && !*cli.Subscription:
			return fmt.Errorf("%s doesn't look signed in to a subscription here: %s (or --force)", options.provider, cli.Note)
		}
	}
	var token string
	if options.tokenStdin {
		if token, err = readToken(); err != nil {
			return err
		}
	}
	client, server, err := launcher.Connect()
	if err != nil {
		return err
	}
	mode := "runner"
	if options.tokenStdin {
		mode = "token"
	}
	var account Account
	body := map[string]any{"provider": options.provider, "authentication_mode": mode}
	if options.name != "" {
		body["display_name"] = options.name
	}
	if options.plan != "" {
		body["plan_name"] = options.plan
	}
	if err := client.Post("/api/provider-accounts", body, &account); err != nil {
		return err
	}
	if account.AuthenticationMode != mode {
		return fmt.Errorf("you already have %s as a %s account; use another --name", account.DisplayName, account.AuthenticationMode)
	}
	fmt.Printf("Account : %s (%s)\n", account.DisplayName, account.ID)
	if options.tokenStdin {
		if !options.yes {
			if !term.IsTerminal(int(os.Stderr.Fd())) {
				return errors.New("pass --yes to store the token without being asked")
			}
			fmt.Fprintf(os.Stderr, "Store this token on %s, encrypted, for the machines you assign %s to? Anyone controlling those machines can use your Copilot subscription. [y/N] ", server, account.DisplayName)
			answer, _ := bufio.NewReader(openTTY()).ReadString('\n')
			if !strings.HasPrefix(strings.ToLower(strings.TrimSpace(answer)), "y") {
				return errors.New("not stored")
			}
		}
		if err := client.Do("PUT", "/api/provider-accounts/"+url.PathEscape(account.ID)+"/credential", map[string]any{"token": token}, nil); err != nil {
			return err
		}
		fmt.Println("Token   : stored, encrypted (it can't be read back)")
	}
	hostname, _ := os.Hostname()
	var enrolled struct {
		Machine struct {
			ID   string `json:"id"`
			Name string `json:"name"`
		} `json:"machine"`
	}
	detected := map[string]any{"auth": cli.Auth, "version": cli.Version}
	err = client.Post("/api/provider-accounts/"+url.PathEscape(account.ID)+"/enrollments", map[string]any{"hostname": hostname, "machine": options.machine, "detected": detected}, &enrolled)
	switch {
	case err == nil:
		fmt.Printf("Machine : %s (%s) runs it now\n", enrolled.Machine.Name, enrolled.Machine.ID)
	case options.tokenStdin && api.StatusOf(err) == 404:
		fmt.Println("Machine : this computer isn't one of your fleet machines; assign the account to machines on the Subscriptions page")
	default:
		return err
	}
	return nil
}

// readToken reads one line from standard input, without echo when it's a terminal.
func readToken() (string, error) {
	if term.IsTerminal(int(os.Stdin.Fd())) {
		fmt.Fprint(os.Stderr, "Fine-grained personal access token (Copilot Requests permission): ")
		data, err := term.ReadPassword(int(os.Stdin.Fd()))
		fmt.Fprintln(os.Stderr)
		if err != nil {
			return "", err
		}
		return strings.TrimSpace(string(data)), nil
	}
	line, err := bufio.NewReader(io.LimitReader(os.Stdin, 8192)).ReadString('\n')
	if err != nil && err != io.EOF {
		return "", err
	}
	if token := strings.TrimSpace(line); token != "" {
		return token, nil
	}
	return "", errors.New("no token on standard input")
}

// openTTY is the terminal to ask on when standard input carried the token.
func openTTY() io.Reader {
	if term.IsTerminal(int(os.Stdin.Fd())) {
		return os.Stdin
	}
	for _, name := range []string{"/dev/tty", "CONIN$"} {
		if file, err := os.Open(name); err == nil {
			return file
		}
	}
	return strings.NewReader("")
}
