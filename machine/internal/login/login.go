// Package login implements OAuth device sign-in and local credential revocation.
package login

import (
	"errors"
	"flag"
	"fmt"
	"os"
	"strings"

	"ai-coding-agent-proxy/machine/internal/settings"
)

func Login(args []string) int {
	flags := flag.NewFlagSet("login", flag.ContinueOnError)
	flags.Usage = func() {
		fmt.Fprintln(flags.Output(), "Usage: login [https://server]\n\nSign in using a browser and a device code. For automation inject an OAuth access token with "+settings.TokenEnvironment+". Passwords, --user and --key are no longer supported.")
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
	server := settings.DefaultServerURL()
	if len(positional) == 1 {
		server = positional[0]
	}
	server = strings.TrimRight(strings.TrimSpace(server), "/")
	if !strings.Contains(server, "://") {
		server = "https://" + server
	}
	entry, err := settings.DeviceLogin(server, os.Stdout)
	if err != nil {
		fmt.Fprintln(os.Stderr, "login:", err)
		return 1
	}
	unlock, err := settings.LockCredentials()
	if err != nil {
		fmt.Fprintln(os.Stderr, "login:", err)
		return 1
	}
	defer unlock()
	credentials := settings.LoadCredentials()
	credentials.Servers[settings.Normalize(server)] = entry
	credentials.Default = settings.Normalize(server)
	if err := credentials.Save(); err != nil {
		fmt.Fprintln(os.Stderr, "login:", err)
		return 1
	}
	fmt.Printf("Signed in to %s as %s.\n", server, entry.User)
	return 0
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
