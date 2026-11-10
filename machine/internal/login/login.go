// Package login implements OAuth device sign-in and local credential revocation.
package login

import (
	"errors"
	"flag"
	"fmt"
	"os"
	"strings"

	"ai-control-mesh/machine/internal/presence"
	"ai-control-mesh/machine/internal/settings"
)

// EnableEnvironment lists the toggles to switch on after a sign-in, as login --enable does: with
// SetupCodeEnvironment, what sets up a container that nobody configures by hand.
const EnableEnvironment = "AI_PROXY_MACHINE_ENABLE"

func Login(args []string) int {
	flags := flag.NewFlagSet("login", flag.ContinueOnError)
	code := flags.String("code", "", "A setup code from the console's Machines page, used instead of approving in a browser (default: $"+settings.SetupCodeEnvironment+")")
	enable := flags.String("enable", "", "Toggles to switch on once signed in, comma-separated: "+strings.Join(settings.Toggles, ", ")+" (default: $"+EnableEnvironment+")")
	flags.Usage = func() {
		fmt.Fprintln(flags.Output(), "Usage: login [https://server] [--code CODE] [--enable LIST]\n\nSign in using a browser and a device code, or with a setup code created on the console's Machines page (Add a machine, Create a setup code). For automation inject an OAuth access token with "+settings.TokenEnvironment+". Passwords, --user and --key are no longer supported.")
		fmt.Fprintln(flags.Output())
		flags.PrintDefaults()
	}
	for _, arg := range args {
		if arg == "-u" || strings.HasPrefix(arg, "--user") || strings.HasPrefix(arg, "--key") {
			fmt.Fprintln(os.Stderr, "Password/key login was removed. Use login [server] for OIDC device sign-in, or "+settings.TokenEnvironment+" for an OAuth service-account access token.")
			return 2
		}
	}
	positional, err := parse(flags, args)
	if errors.Is(err, flag.ErrHelp) {
		return 0
	}
	if err != nil || len(positional) > 1 {
		return 2
	}
	given := map[string]bool{}
	flags.Visit(func(f *flag.Flag) { given[f.Name] = true })
	setupCode := *code
	if !given["code"] {
		setupCode = os.Getenv(settings.SetupCodeEnvironment)
	}
	list := *enable
	if !given["enable"] {
		list = os.Getenv(EnableEnvironment)
	}
	// Checked before signing in: a typo mustn't spend a setup code's use on a half-done setup.
	names, err := settings.ParseToggles(list)
	if err != nil {
		fmt.Fprintln(os.Stderr, "login:", err)
		return 2
	}
	server := settings.DefaultServerURL()
	if len(positional) == 1 {
		server = positional[0]
	}
	if err := SignIn(serverURL(server), strings.TrimSpace(setupCode), names); err != nil {
		fmt.Fprintln(os.Stderr, "login:", err)
		return 1
	}
	return 0
}

// serverURL is a server as given, https:// when it has no scheme.
func serverURL(server string) string {
	server = strings.TrimRight(strings.TrimSpace(server), "/")
	if !strings.Contains(server, "://") {
		server = "https://" + server
	}
	return server
}

// SignIn signs in to server (approved by setupCode, or in a browser when it's empty), saves the
// credentials as the default server's, then switches on the toggles named in enable.
func SignIn(server, setupCode string, enable []string) error {
	entry, err := settings.DeviceLogin(server, setupCode, os.Stdout)
	if err != nil {
		return err
	}
	unlock, err := settings.LockCredentials()
	if err != nil {
		return err
	}
	credentials := settings.LoadCredentials()
	credentials.Servers[settings.Normalize(server)] = entry
	credentials.Default = settings.Normalize(server)
	err = credentials.Save()
	unlock()
	if err != nil {
		return err
	}
	fmt.Printf("Signed in to %s as %s.\n", server, entry.User)
	return Enable(enable)
}

// Enable switches toggles on, as `agent-machine <name> on` does for each, notes included.
func Enable(names []string) error {
	if len(names) == 0 {
		return nil
	}
	machine := settings.LoadMachine()
	for _, name := range names {
		machine.SetToggle(name, true)
	}
	if err := machine.Save(); err != nil {
		return err
	}
	fmt.Printf("Switched on: %s.\n", strings.Join(names, ", "))
	for _, name := range names {
		for _, note := range machine.ToggleNotes(name) {
			fmt.Println(note)
		}
		if _, err := presence.AgentCommand(); name == "launches" && err != nil {
			fmt.Println("Note:", err)
		}
	}
	return nil
}

// Automatic signs in to server with the setup code in SetupCodeEnvironment when there are no
// credentials for it yet: a container's first start (agent-machine run or start). Later starts find
// the credentials and leave the code alone, so a restarted container doesn't spend another use.
func Automatic(server string) error {
	code := strings.TrimSpace(os.Getenv(settings.SetupCodeEnvironment))
	// Agents and terminals started from here inherit this environment: keep the secret out of it.
	_ = os.Unsetenv(settings.SetupCodeEnvironment)
	if code == "" || os.Getenv(settings.TokenEnvironment) != "" {
		return nil
	}
	server = serverURL(server)
	if entry := settings.LoadCredentials().Servers[settings.Normalize(server)]; entry != nil && (entry.AccessToken != "" || entry.RefreshToken != "") {
		return nil
	}
	names, err := settings.ParseToggles(os.Getenv(EnableEnvironment))
	if err != nil {
		return fmt.Errorf("%s: %w", EnableEnvironment, err)
	}
	fmt.Printf("Signing in to %s with the setup code from %s.\n", server, settings.SetupCodeEnvironment)
	return SignIn(server, code, names)
}

func Logout(args []string) int {
	flags := flag.NewFlagSet("logout", flag.ContinueOnError)
	positional, err := parse(flags, args)
	if errors.Is(err, flag.ErrHelp) {
		return 0
	}
	if err != nil || len(positional) > 1 {
		return 2
	}
	unlock, err := settings.LockCredentials()
	if err != nil {
		fmt.Fprintln(os.Stderr, "logout:", err)
		return 1
	}
	defer unlock()
	credentials := settings.LoadCredentials()
	key := credentials.Default
	if len(positional) == 1 {
		key = settings.Normalize(positional[0])
	}
	entry := credentials.Servers[key]
	if entry == nil {
		fmt.Println("Not signed in to that server.")
		return 0
	}
	revokeErr := settings.Revoke(entry)
	delete(credentials.Servers, key)
	if credentials.Default == key {
		credentials.Default = ""
		for other := range credentials.Servers {
			credentials.Default = other
			break
		}
	}
	if err := credentials.Save(); err != nil {
		fmt.Fprintln(os.Stderr, "logout:", err)
		return 1
	}
	fmt.Printf("Signed out of %s.\n", entry.URL)
	if revokeErr != nil {
		fmt.Fprintln(os.Stderr, "logout: local credentials forgotten, but issuer revocation failed:", revokeErr)
		return 1
	}
	return 0
}

func parse(flags *flag.FlagSet, args []string) ([]string, error) {
	var positional []string
	for {
		if err := flags.Parse(args); err != nil {
			return nil, err
		}
		args = flags.Args()
		if len(args) == 0 {
			return positional, nil
		}
		positional = append(positional, args[0])
		args = args[1:]
	}
}
