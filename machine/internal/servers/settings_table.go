package servers

// A model server's settings, as environment variables agent-machine starts it with: a
// table of known options the console shows and checks, and extra variables of the
// server's own prefix. Ollama's (ollama_settings.go) and llama.cpp's (llamacpp_settings.go)
// are each one table; the server can't set anything else a program here runs with.

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"ai-control-mesh/machine/internal/settings"
)

// Setting is one option of a model server, written to the environment variables in Env.
type Setting struct {
	Key      string   `json:"key"`
	Group    string   `json:"group"`
	Label    string   `json:"label"`
	Kind     string   `json:"kind"` // bool, int, str or choice
	Default  any      `json:"default"`
	Help     string   `json:"help"`
	Env      []string `json:"env"`
	Choices  []string `json:"choices"`
	Editable bool     `json:"editable"` // a choice that also takes free text
	Minimum  int64    `json:"minimum"`
	Maximum  int64    `json:"maximum"`
	// Scale multiplies an int when it's written to the environment.
	Scale int64 `json:"-"`
	// Bool is how a bool is written: "0"/"1" unless set.
	False, True string `json:"-"`
}

const maxInt = 1<<31 - 1

func setting(key, group, label, kind string, def any, help string, env ...string) Setting {
	return Setting{Key: key, Group: group, Label: label, Kind: kind, Default: def, Help: help, Env: env, Choices: []string{}, Maximum: maxInt, Scale: 1}
}

func (s Setting) choices(editable bool, values ...string) Setting {
	s.Choices, s.Editable = values, editable
	return s
}

func (s Setting) bounds(minimum, maximum int64) Setting {
	s.Minimum, s.Maximum = minimum, maximum
	return s
}

// table is one model server's settings.
type table struct {
	settings []Setting
	groups   []string
	// extra says why a variable can't be set as an extra one, or "" when it can.
	extra func(name string) string
	// file is the settings' file in agent-machine's folder.
	file string
}

func (t table) path() string { return filepath.Join(settings.Home(), t.file) }

// defaults is every setting at its default, and no extra variables.
func (t table) defaults() map[string]any {
	config := map[string]any{"extra_env": ""}
	for _, s := range t.settings {
		config[s.Key] = s.Default
	}
	return config
}

// parseExtra reads KEY=VALUE lines; a blank value unsets the variable, '#' starts a comment.
func (t table) parseExtra(text string) (map[string]*string, error) {
	result := map[string]*string{}
	for number, raw := range strings.Split(text, "\n") {
		line := strings.TrimSpace(raw)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		name, value, found := strings.Cut(line, "=")
		name = strings.TrimSpace(name)
		if !found || name == "" || strings.ContainsAny(name, " \t") {
			return nil, fmt.Errorf("Extra environment line %d is not KEY=VALUE: %q", number+1, strings.TrimRight(raw, "\r"))
		}
		if reason := t.extra(name); reason != "" {
			return nil, fmt.Errorf("Extra environment line %d sets %s: %s", number+1, name, reason)
		}
		if value = strings.TrimSpace(value); value == "" {
			result[name] = nil
		} else {
			result[name] = &value
		}
	}
	return result, nil
}

func asInt(value any) (int64, bool) {
	switch typed := value.(type) {
	case float64:
		if typed != float64(int64(typed)) {
			return 0, false
		}
		return int64(typed), true
	case int64:
		return typed, true
	case int:
		return int64(typed), true
	case string:
		parsed, err := strconv.ParseInt(strings.TrimSpace(typed), 10, 64)
		return parsed, err == nil
	case bool:
		if typed {
			return 1, true
		}
		return 0, true
	}
	return 0, false
}

func asText(value any) string {
	switch typed := value.(type) {
	case nil:
		return ""
	case string:
		return typed
	case float64:
		return strconv.FormatFloat(typed, 'f', -1, 64)
	}
	return fmt.Sprint(value)
}

func truthy(value any) bool {
	switch typed := value.(type) {
	case bool:
		return typed
	case string:
		return typed != ""
	case nil:
		return false
	}
	number, ok := asInt(value)
	return !ok || number != 0
}

func contains(values []string, value string) bool {
	for _, item := range values {
		if item == value {
			return true
		}
	}
	return false
}

// validate checks and tidies a config from the console against the table.
func (t table) validate(data map[string]any) (map[string]any, error) {
	if data == nil {
		return nil, errors.New("Configuration must be an object")
	}
	config := t.defaults()
	for _, s := range t.settings {
		value, present := data[s.Key]
		if !present {
			continue
		}
		switch s.Kind {
		case "bool":
			config[s.Key] = truthy(value)
		case "int":
			number, ok := asInt(value)
			if !ok {
				return nil, fmt.Errorf("%s must be a number", s.Label)
			}
			if number < s.Minimum || number > s.Maximum {
				return nil, fmt.Errorf("%s must be between %d and %d", s.Label, s.Minimum, s.Maximum)
			}
			config[s.Key] = number
		case "choice":
			text := strings.TrimSpace(asText(value))
			if !s.Editable && len(s.Choices) > 0 && !contains(s.Choices, text) {
				return nil, fmt.Errorf("%s must be one of: %s", s.Label, strings.Join(s.Choices, ", "))
			}
			// A value typed in is one word: it goes into one environment variable.
			if strings.ContainsAny(text, "\r\n") {
				return nil, fmt.Errorf("%s must be one line", s.Label)
			}
			config[s.Key] = text
		default:
			text := strings.TrimSpace(asText(value))
			if strings.ContainsAny(text, "\r\n") {
				return nil, fmt.Errorf("%s must be one line", s.Label)
			}
			config[s.Key] = text
		}
	}
	config["extra_env"] = asText(data["extra_env"])
	// A bad line is refused on saving rather than on starting.
	if _, err := t.parseExtra(config["extra_env"].(string)); err != nil {
		return nil, err
	}
	return config, nil
}

// managed is the variables the config sets; nil means unset.
func (t table) managed(config map[string]any) map[string]*string {
	env := map[string]*string{}
	for _, s := range t.settings {
		value, present := config[s.Key]
		if !present {
			value = s.Default
		}
		var text *string
		switch s.Kind {
		case "bool":
			flag := "0"
			if s.False != "" {
				flag = s.False
			}
			if truthy(value) {
				flag = "1"
				if s.True != "" {
					flag = s.True
				}
			}
			text = &flag
		case "int":
			number, _ := asInt(value)
			formatted := strconv.FormatInt(number*s.Scale, 10)
			text = &formatted
		default:
			if trimmed := strings.TrimSpace(asText(value)); trimmed != "" {
				text = &trimmed
			}
		}
		for _, name := range s.Env {
			env[name] = text
		}
	}
	extra, _ := t.parseExtra(asText(config["extra_env"]))
	for name, value := range extra {
		env[name] = value
	}
	return env
}

// environment is the environment the server starts with: base without the managed variables
// and the ones drop names (where it listens, set by agent-machine), with the managed ones applied.
func (t table) environment(base []string, config map[string]any, drop func(string) bool) []string {
	managed := t.managed(config)
	result := []string{}
	for _, entry := range base {
		name, _, _ := strings.Cut(entry, "=")
		if _, ours := managed[name]; ours || drop(name) {
			continue
		}
		result = append(result, entry)
	}
	names := make([]string, 0, len(managed))
	for name := range managed {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		if value := managed[name]; value != nil {
			result = append(result, name+"="+*value)
		}
	}
	return result
}

// load is the saved config over the defaults; a missing or broken file is the defaults.
func (t table) load() map[string]any {
	config := t.defaults()
	data, err := os.ReadFile(t.path())
	if err != nil {
		return config
	}
	var saved map[string]any
	if json.Unmarshal(data, &saved) != nil {
		return config
	}
	if valid, err := t.validate(saved); err == nil {
		return valid
	}
	return config
}

// save validates a config and saves it.
func (t table) save(data map[string]any) (map[string]any, error) {
	config, err := t.validate(data)
	if err != nil {
		return nil, err
	}
	encoded, _ := json.MarshalIndent(config, "", "  ")
	if err := os.MkdirAll(settings.Home(), 0o700); err != nil {
		return nil, err
	}
	temporary := t.path() + ".tmp"
	if err := os.WriteFile(temporary, append(encoded, '\n'), 0o600); err != nil {
		return nil, err
	}
	return config, os.Rename(temporary, t.path())
}

// publicEnv is a managed environment as JSON: a string, or null for unset.
func publicEnv(env map[string]*string) map[string]any {
	public := map[string]any{}
	for name, value := range env {
		if value == nil {
			public[name] = nil
		} else {
			public[name] = *value
		}
	}
	return public
}
