package command

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"ai-coding-agent-proxy/machine/internal/mcp"
)

// mcpCommand lists, adds and removes the MCP servers this machine shares (mcp.json).
func mcpCommand(args []string) int {
	if len(args) == 0 {
		return listMCP()
	}
	switch args[0] {
	case "add":
		server, err := parseMCP(args[1:])
		if err != nil {
			return fail(err.Error())
		}
		if err := mcp.Add(server); err != nil {
			return fail(err.Error())
		}
		fmt.Printf("Sharing %s. Agents launched with --mcp %s (or chosen on Start an agent) can use it, within a minute.\n", server.Name, server.Name)
		if !server.Found() {
			fmt.Printf("Note: %s isn't on this machine's PATH yet.\n", server.Command[0])
		}
		return listMCP()
	case "remove":
		if len(args) != 2 {
			return fail("mcp remove takes a name")
		}
		removed, err := mcp.Remove(args[1])
		if err != nil {
			return fail(err.Error())
		}
		if !removed {
			return fail("this machine shares no MCP server called " + args[1])
		}
		return listMCP()
	}
	return fail("mcp takes add or remove")
}

func parseMCP(args []string) (mcp.Server, error) {
	server := mcp.Server{}
	if len(args) == 0 || strings.HasPrefix(args[0], "-") {
		return server, fmt.Errorf("mcp add takes a name first")
	}
	server.Name = args[0]
	for index := 1; index < len(args); index++ {
		arg := args[index]
		if arg == "--" {
			server.Command = args[index+1:]
			break
		}
		// --name value or --name=value (--env=NAME=VALUE included).
		name, value, hasValue := strings.Cut(arg, "=")
		if !hasValue {
			if index+1 >= len(args) {
				return server, fmt.Errorf("%s needs a value", arg)
			}
			value = args[index+1]
			index++
		}
		switch name {
		case "--url":
			server.URL = value
		case "--dir":
			dir, err := filepath.Abs(value)
			if err != nil {
				return server, err
			}
			if info, err := os.Stat(dir); err != nil || !info.IsDir() {
				return server, fmt.Errorf("%s isn't a folder", dir)
			}
			server.Dir = dir
		case "--description":
			server.Description = value
		case "--env":
			key, setting, ok := strings.Cut(value, "=")
			if !ok || key == "" {
				return server, fmt.Errorf("--env takes NAME=VALUE")
			}
			if server.Env == nil {
				server.Env = map[string]string{}
			}
			server.Env[key] = setting
		case "--header":
			key, setting, ok := strings.Cut(value, ":")
			if !ok || strings.TrimSpace(key) == "" {
				return server, fmt.Errorf(`--header takes "Name: value"`)
			}
			if server.Headers == nil {
				server.Headers = map[string]string{}
			}
			server.Headers[strings.TrimSpace(key)] = strings.TrimSpace(setting)
		default:
			return server, fmt.Errorf("unknown option %s (the command goes after --)", arg)
		}
	}
	return server, server.Check()
}

func listMCP() int {
	servers, err := mcp.Load()
	if err != nil {
		fmt.Fprintf(os.Stderr, "agent-machine: %v\n", err)
		return 1
	}
	if len(servers) == 0 {
		fmt.Println("MCP       : none shared (agent-machine mcp add <name> -- <command>)")
		return 0
	}
	for index, server := range servers {
		label := "MCP       :"
		if index > 0 {
			label = "           "
		}
		fmt.Println(label, describeMCP(server))
	}
	return 0
}

func describeMCP(server mcp.Server) string {
	how := server.URL
	if server.URL == "" {
		how = strings.Join(server.Command, " ")
		if !server.Found() {
			how += " (not installed)"
		}
	}
	return fmt.Sprintf("%s (%s): %s", server.Name, server.Transport(), how)
}
