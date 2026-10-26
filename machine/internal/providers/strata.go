package providers

// Strata serves one local model and asks for an API key on /v1. Its Desktop app and
// Terminal share these places (strata_control.profiles, .runtime and .paths in Strata's
// install):
//
//	profiles  %APPDATA%\Strata\profiles\<id>.json, else $XDG_CONFIG_HOME/strata/profiles.
//	          "source_config" names the run config: host, port, model_name, api_key, log.
//	runtime   %LOCALAPPDATA%\Strata\runtime, else $XDG_STATE_HOME/strata/runtime.
//	          <id>-<hex>.ready.json exists while an instance serves (api_url, model_id,
//	          model_state, pid); <id>.server.log is that instance's request log.
//
// Only Strata's files are read here; package servers starts and stops Strata through its
// own `strata runtime` command.

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"ai-coding-agent-proxy/machine/internal/process"
)

// Instance is one saved Strata profile and, while it serves, its live instance.
type Instance struct {
	Profile    string
	URL        string
	Running    bool
	Model      any
	ModelState any
	PID        any
	ConfigPath string
	EngineLog  string
	ServerLog  string
	// APIKey is never sent to the console; the server gets it in the hello to forward traffic.
	APIKey string
}

// Public is the instance for the console.
func (i Instance) Public() map[string]any {
	logs := []string{}
	if i.ServerLog != "" {
		logs = append(logs, "server")
	}
	if i.EngineLog != "" {
		logs = append(logs, "engine")
	}
	var config any
	if i.ConfigPath != "" {
		config = i.ConfigPath
	}
	return map[string]any{
		"profile": i.Profile, "url": i.URL, "running": i.Running, "model": i.Model, "model_state": i.ModelState,
		"pid": i.PID, "config_path": config, "has_key": i.APIKey != "", "logs": logs,
	}
}

// Port is where the instance serves.
func (i Instance) Port() int {
	parsed, err := url.Parse(i.URL)
	if err != nil {
		return 0
	}
	port, _ := strconv.Atoi(parsed.Port())
	return port
}

func strataProfileDir() string {
	if dir := os.Getenv("STRATA_PROFILE_DIR"); dir != "" {
		return dir
	}
	if runtime.GOOS == "windows" {
		base := os.Getenv("APPDATA")
		if base == "" {
			home, _ := os.UserHomeDir()
			base = filepath.Join(home, "AppData", "Roaming")
		}
		return filepath.Join(base, "Strata", "profiles")
	}
	base := os.Getenv("XDG_CONFIG_HOME")
	if base == "" {
		home, _ := os.UserHomeDir()
		base = filepath.Join(home, ".config")
	}
	return filepath.Join(base, "strata", "profiles")
}

func strataRuntimeDir() string {
	if dir := os.Getenv("STRATA_RUNTIME_DIR"); dir != "" {
		return dir
	}
	if runtime.GOOS == "windows" {
		base := os.Getenv("LOCALAPPDATA")
		if base == "" {
			home, _ := os.UserHomeDir()
			base = filepath.Join(home, "AppData", "Local")
		}
		return filepath.Join(base, "Strata", "runtime")
	}
	base := os.Getenv("XDG_STATE_HOME")
	if base == "" {
		home, _ := os.UserHomeDir()
		base = filepath.Join(home, ".local", "state")
	}
	return filepath.Join(base, "strata", "runtime")
}

// FirstKey is the first key a Strata server accepts, in its forms: "k1,k2", a list, or "none" for no key.
func FirstKey(value any) string {
	var keys []string
	switch typed := value.(type) {
	case []any:
		for _, item := range typed {
			keys = append(keys, strings.TrimSpace(str(item)))
		}
	case string:
		if strings.ToLower(strings.TrimSpace(typed)) == "none" {
			return ""
		}
		for _, item := range strings.Split(typed, ",") {
			keys = append(keys, strings.TrimSpace(item))
		}
	}
	for _, key := range keys {
		if key != "" {
			return key
		}
	}
	return ""
}

func readJSON(path string) map[string]any {
	data, err := os.ReadFile(path)
	if err != nil {
		return map[string]any{}
	}
	var value any
	if json.Unmarshal(data, &value) != nil {
		return map[string]any{}
	}
	return object(value)
}

// Discover is the saved Strata profiles on this machine, serving ones first.
func Discover() []Instance {
	paths, _ := filepath.Glob(filepath.Join(strataProfileDir(), "*.json"))
	sort.Strings(paths)
	runtimeDir := strataRuntimeDir()
	found := []Instance{}
	for _, path := range paths {
		if strings.HasPrefix(filepath.Base(path), ".") {
			continue
		}
		profile := readJSON(path)
		if len(profile) == 0 {
			continue
		}
		name := str(profile["id"])
		if name == "" {
			name = strings.TrimSuffix(filepath.Base(path), ".json")
		}
		source := str(profile["source_config"])
		config := map[string]any{}
		if source != "" {
			config = readJSON(source)
		}
		for key, value := range object(profile["overrides"]) {
			config[key] = value
		}
		ready := readyFile(runtimeDir, name)
		address := str(ready["api_url"])
		if address == "" {
			address = configURL(config)
		}
		if address == "" {
			continue
		}
		instance := Instance{
			Profile: name, URL: strings.TrimRight(address, "/"), Running: len(ready) > 0,
			Model: ready["model_id"], ConfigPath: source, APIKey: FirstKey(config["api_key"]),
		}
		if instance.Model == nil || instance.Model == "" {
			instance.Model = config["model_name"]
		}
		if len(ready) > 0 {
			instance.ModelState, instance.PID = ready["model_state"], ready["pid"]
		}
		if serverLog := filepath.Join(runtimeDir, name+".server.log"); isFile(serverLog) {
			instance.ServerLog = serverLog
		}
		if engineLog := str(config["log"]); engineLog != "" && isFile(engineLog) {
			instance.EngineLog = engineLog
		}
		found = append(found, instance)
	}
	sort.SliceStable(found, func(i, j int) bool { return found[i].Running && !found[j].Running })
	return found
}

func isFile(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir()
}

// readyFile is the newest ready file for a profile whose process is still alive; a crash
// can leave a stale one behind.
func readyFile(dir, profile string) map[string]any {
	candidates, _ := filepath.Glob(filepath.Join(dir, profile+"-*.ready.json"))
	newest := map[string]any{}
	for _, path := range candidates {
		ready := readJSON(path)
		pid, ok := ready["pid"].(float64)
		if !ok || pid != float64(int(pid)) || !process.Alive(int(pid)) {
			continue
		}
		if len(newest) == 0 || number(ready["created_at"]) > number(newest["created_at"]) {
			newest = ready
		}
	}
	return newest
}

func configURL(config map[string]any) string {
	port, ok := config["port"].(float64)
	if !ok || port != float64(int(port)) {
		return ""
	}
	host := str(config["host"])
	if host == "" || host == "0.0.0.0" || host == "::" {
		host = "127.0.0.1"
	}
	return "http://" + net.JoinHostPort(host, strconv.Itoa(int(port)))
}

var loopbackHosts = map[string]bool{"localhost": true, "127.0.0.1": true, "::1": true, "0.0.0.0": true, "::": true}

// InstanceOn is the profile serving on this port on loopback, if any.
func InstanceOn(port int) *Instance {
	for _, instance := range Discover() {
		parsed, err := url.Parse(instance.URL)
		if err == nil && loopbackHosts[parsed.Hostname()] && instance.Port() == port {
			found := instance
			return &found
		}
	}
	return nil
}

// strataKey is the key for the Strata on port and where it came from: the server's (for
// its containers), STRATA_API_KEY, or the profile serving there.
func strataKey(port int, given string) (string, any) {
	if given != "" {
		return given, "container"
	}
	if key := FirstKey(os.Getenv("STRATA_API_KEY")); key != "" {
		return key, "environment"
	}
	if instance := InstanceOn(port); instance != nil && instance.APIKey != "" {
		return instance.APIKey, "profile " + instance.Profile
	}
	return "", nil
}

func strataStatus(base string, port int, given string) map[string]any {
	key, source := strataKey(port, given)
	headers := map[string]string{}
	if key != "" {
		headers["Authorization"] = "Bearer " + key
	}
	var instance any
	if found := InstanceOn(port); found != nil {
		instance = found.Public()
	}
	unknown := map[string]any{"state": "unknown", "source": source}
	health, _, err := getJSON(base+"/health", nil, 4*time.Second)
	if err != nil {
		return map[string]any{"online": false, "error": err.Error(), "key": unknown, "models": []any{}, "instance": instance}
	}
	facts := object(health)
	if facts["service"] != "strata" {
		return map[string]any{"online": false, "error": "It answered, but not as a Strata server.", "key": unknown, "models": []any{}, "instance": instance}
	}
	var (
		wait                   sync.WaitGroup
		status, models         any
		statusCode, modelsCode int
		statusErr, modelsErr   error
	)
	wait.Add(2)
	go func() {
		defer wait.Done()
		status, statusCode, statusErr = getJSON(base+"/v1/status", headers, 4*time.Second)
	}()
	go func() {
		defer wait.Done()
		models, modelsCode, modelsErr = getJSON(base+"/v1/models", headers, 4*time.Second)
	}()
	wait.Wait()
	if statusErr != nil || modelsErr != nil {
		failure := statusErr
		if failure == nil {
			failure = modelsErr
		}
		return map[string]any{"online": false, "error": failure.Error(), "health": facts, "key": unknown, "models": []any{}, "instance": instance}
	}
	state := "not_required"
	if statusCode == http.StatusUnauthorized {
		state = "missing"
		if key != "" {
			state = "rejected"
		}
	} else if truthy(facts["api_key"]) {
		state = "ok"
	}
	var listed []any
	if modelsCode < 400 {
		listed = list(object(models)["data"])
	}
	if listed == nil && str(facts["model"]) != "" {
		// Without a key, /health still names the model.
		value := "unloaded"
		if truthy(facts["loaded"]) {
			value = "loaded"
		}
		listed = []any{map[string]any{"id": facts["model"], "status": map[string]any{"value": value}, "meta": map[string]any{"n_ctx": facts["max_context"]}}}
	}
	kept := []any{}
	for _, item := range listed {
		if str(object(item)["id"]) != "" {
			kept = append(kept, item)
		}
	}
	var statusBody any
	if statusCode < 400 {
		statusBody = object(status)
	}
	return map[string]any{
		"online": true, "health": facts, "status": statusBody,
		"key": map[string]any{"state": state, "source": source}, "models": kept, "instance": instance,
	}
}

func truthy(value any) bool {
	switch typed := value.(type) {
	case bool:
		return typed
	case string:
		return typed != ""
	case float64:
		return typed != 0
	}
	return value != nil
}

// strataControl loads or unloads the model. Strata accepts these from a JSON client without a browser Origin.
func strataControl(base string, port int, given, action string) (string, error) {
	key, _ := strataKey(port, given)
	headers := map[string]string{}
	if key != "" {
		headers["Authorization"] = "Bearer " + key
	}
	response, err := send(http.MethodPost, base+"/v1/"+action, map[string]any{}, headers, 2*time.Minute)
	if err != nil {
		return "", unavailable("Strata", err)
	}
	defer response.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if response.StatusCode >= 400 {
		return "", errors.New(errorText(data, response.StatusCode, "Strata"))
	}
	var body any
	_ = json.Unmarshal(data, &body)
	if status := str(object(body)["status"]); status != "" {
		return status, nil
	}
	return action, nil
}

// strataLog is the end of the server or engine log of the profile serving on port, with its key blanked out.
func strataLog(port int, source string, tail int) (string, error) {
	if source == "" {
		source = "server"
	}
	if source != "server" && source != "engine" {
		return "", errors.New("Log source must be server or engine")
	}
	instance := InstanceOn(port)
	if instance == nil {
		return "", fmt.Errorf("No Strata profile on this machine serves on port %d, so its logs can't be read.", port)
	}
	path := instance.ServerLog
	if source == "engine" {
		path = instance.EngineLog
	}
	if path == "" {
		return "", nil
	}
	text, err := tailFile(path, tail)
	if err != nil {
		return "", nil
	}
	if instance.APIKey != "" {
		text = strings.ReplaceAll(text, instance.APIKey, "<api key>")
	}
	return text, nil
}
