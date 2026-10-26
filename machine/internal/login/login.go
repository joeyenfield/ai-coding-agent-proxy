// Package login is `agent-machine login` and `agent-machine logout`: signing this machine in to an agent-server.
//
// Signing in trades a username and password for a token, kept with the server's URL in
// agent-machine's own folder (credentials.json, apart from the agent command's sign-in);
// the password isn't stored. `agent-machine run` reads the file each time it connects:
// it joins the last server signed in to and signs with its token. A server without
// accounts is remembered too, with its access key when one is given (--key).
package login

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"golang.org/x/term"

	"ai-coding-agent-proxy/machine/internal/settings"
)

// Login is `agent-machine login [url] [--user name] [--key key]`.
func Login(args []string) int {
	defaultURL := settings.DefaultServerURL()
	flags := flag.NewFlagSet("agent-machine login", flag.ContinueOnError)
	flags.Usage = func() {
		fmt.Fprintf(flags.Output(), "Usage: agent-machine login [url] [--user name] [--key access-key]\n\n"+
			"Sign this machine in to an agent-server, to supply its LLM providers (and take launches) there.\n"+
			"url defaults to %s.\n\n", defaultURL)
		flags.PrintDefaults()
	}
	user := flags.String("user", "", "Your username (asked for when omitted)")
	flags.StringVar(user, "u", "", "Short for --user")
	key := flags.String("key", "", "For a server without accounts: its access key, to use it from another machine")
	positional, err := parse(flags, args)
	if err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 2
	}
	server := defaultURL
	if len(positional) > 0 {
		server = positional[0]
	}
	server = strings.TrimRight(strings.TrimSpace(server), "/")
	if !strings.Contains(server, "://") {
		server = "http://" + server
	}
	client := settings.ServerClient(20 * time.Second)
	var status struct {
		Required bool `json:"required"`
	}
	if err := getJSON(client, server+"/api/auth/status", &status); err != nil {
		fmt.Fprintf(os.Stderr, "agent-machine login: couldn't reach %s: %v\n", server, err)
		return 1
	}
	credentials := settings.LoadCredentials()
	if !status.Required {
		credentials.Servers[settings.Normalize(server)] = &settings.ServerEntry{URL: server, Token: *key}
		credentials.Default = settings.Normalize(server)
		if err := credentials.Save(); err != nil {
			fmt.Fprintf(os.Stderr, "agent-machine login: %v\n", err)
			return 1
		}
		fmt.Printf("%s doesn't use accounts, so there's no sign-in; agent-machine supplies it from now on.\n", server)
		return 0
	}
	if !strings.HasPrefix(server, "https://") && !settings.IsLoopbackURL(server) {
		fmt.Fprintln(os.Stderr, "Warning: this connection isn't encrypted; your password crosses the network in the clear unless you're on a VPN such as Tailscale.")
	}
	stdin := bufio.NewReader(os.Stdin)
	name := strings.TrimSpace(*user)
	if name == "" {
		fmt.Print("Username: ")
		line, _ := stdin.ReadString('\n')
		name = strings.TrimSpace(line)
	}
	password := os.Getenv("AI_PROXY_PASSWORD_INPUT")
	if password == "" {
		fmt.Print("Password: ")
		if term.IsTerminal(int(os.Stdin.Fd())) {
			typed, err := term.ReadPassword(int(os.Stdin.Fd()))
			fmt.Println()
			if err != nil {
				fmt.Fprintf(os.Stderr, "agent-machine login: %v\n", err)
				return 1
			}
			password = string(typed)
		} else {
			line, _ := stdin.ReadString('\n')
			password = strings.TrimRight(line, "\r\n")
		}
	}
	hostname, _ := os.Hostname()
	body, _ := json.Marshal(map[string]string{"username": name, "password": password, "label": "agent-machine on " + hostname})
	response, err := client.Post(server+"/api/auth/token", "application/json", bytes.NewReader(body))
	if err != nil {
		fmt.Fprintf(os.Stderr, "agent-machine login: %v\n", err)
		return 1
	}
	defer response.Body.Close()
	data, _ := io.ReadAll(response.Body)
	if response.StatusCode >= 400 {
		fmt.Fprintf(os.Stderr, "agent-machine login: %s\n", settings.Detail(data))
		return 1
	}
	var token struct {
		ID    string `json:"id"`
		Token string `json:"token"`
		User  string `json:"user"`
	}
	if err := json.Unmarshal(data, &token); err != nil {
		fmt.Fprintf(os.Stderr, "agent-machine login: unexpected answer: %v\n", err)
		return 1
	}
	credentials.Servers[settings.Normalize(server)] = &settings.ServerEntry{URL: server, User: token.User, Token: token.Token, TokenID: token.ID}
	credentials.Default = settings.Normalize(server)
	if err := credentials.Save(); err != nil {
		fmt.Fprintf(os.Stderr, "agent-machine login: %v\n", err)
		return 1
	}
	fmt.Printf("Signed in to %s as %s. agent-machine supplies this machine's providers there from now on (agent-machine logout to stop). Start it with: agent-machine start\n", server, token.User)
	return 0
}

// Logout is `agent-machine logout [url]`: forget the sign-in, and revoke its token on the server.
func Logout(args []string) int {
	flags := flag.NewFlagSet("agent-machine logout", flag.ContinueOnError)
	flags.Usage = func() {
		fmt.Fprintln(flags.Output(), "Usage: agent-machine logout [url]\n\nForget this machine's sign-in to an agent-server (default: the one signed in to last).")
	}
	positional, err := parse(flags, args)
	if err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 2
	}
	credentials := settings.LoadCredentials()
	key := credentials.Default
	if len(positional) > 0 {
		key = settings.Normalize(positional[0])
	}
	entry := credentials.Servers[key]
	if entry == nil {
		fmt.Println("Not signed in to that server.")
		return 0
	}
	delete(credentials.Servers, key)
	if entry.Token != "" && entry.TokenID != "" {
		// Best effort: the server may be gone.
		request, err := http.NewRequest(http.MethodDelete, strings.TrimRight(entry.URL, "/")+"/api/auth/tokens/"+entry.TokenID, nil)
		if err == nil {
			request.Header.Set(settings.TokenHeader, entry.Token)
			if response, err := settings.ServerClient(10 * time.Second).Do(request); err == nil {
				response.Body.Close()
			}
		}
	}
	if credentials.Default == key {
		credentials.Default = ""
		for other := range credentials.Servers {
			credentials.Default = other
			break
		}
	}
	if err := credentials.Save(); err != nil {
		fmt.Fprintf(os.Stderr, "agent-machine logout: %v\n", err)
		return 1
	}
	fmt.Printf("Signed out of %s.\n", entry.URL)
	return 0
}

// parse allows flags before and after the positional arguments, as argparse does.
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

func getJSON(client *http.Client, url string, out any) error {
	response, err := client.Get(url)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode >= 400 {
		data, _ := io.ReadAll(response.Body)
		return errors.New(settings.Detail(data))
	}
	return json.NewDecoder(response.Body).Decode(out)
}
