package containers

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

const (
	// Prefix names every container the machine runs for the server.
	Prefix = "ai-proxy-"
	label  = "ai-proxy.backend"
)

var (
	// A plain image reference (registry/name:tag or @digest); never something the CLI reads as a flag.
	imagePattern  = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$`)
	volumePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`)
	gpusPattern   = regexp.MustCompile(`^(all|none|\d+(,\d+)*)$`)
	envPattern    = regexp.MustCompile(`^[^\x00-\x1f]{0,200}$`)
	namePattern   = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$`)
	keyPattern    = regexp.MustCompile(`^[A-Za-z0-9_-]{0,100}$`)
	drivePattern  = regexp.MustCompile(`^[A-Za-z]:[\\/]`)
)

// Spec is a container as the server describes it, checked by Validate.
type Spec struct {
	Engine string            `json:"engine"`
	Image  string            `json:"image"`
	Port   int               `json:"port"`
	GPUs   string            `json:"gpus"`
	Env    map[string]string `json:"env"`
	Volume string            `json:"volume,omitempty"`
}

// Validate checks a spec the way the server does, filling in the engine's defaults.
func Validate(raw map[string]any) (Spec, error) {
	if raw == nil {
		return Spec{}, errors.New("container must be an object")
	}
	engine := EngineByID(text(raw["engine"]))
	if engine == nil {
		ids := make([]string, len(Engines))
		for index, item := range Engines {
			ids[index] = item.ID
		}
		return Spec{}, fmt.Errorf("Unknown engine; use one of: %s", strings.Join(ids, ", "))
	}
	image := strings.TrimSpace(text(raw["image"]))
	if image == "" {
		image = engine.Image
	}
	if !imagePattern.MatchString(image) {
		return Spec{}, fmt.Errorf("Image must be an image name such as %s", engine.Image)
	}
	port := engine.Port
	if value, present := raw["port"]; present && value != nil && text(value) != "" && text(value) != "0" {
		parsed, err := strconv.ParseFloat(text(value), 64)
		if err != nil {
			return Spec{}, errors.New("Port must be a number")
		}
		port = int(parsed)
	}
	if port < 1024 || port > 65535 {
		return Spec{}, errors.New("Port must be between 1024 and 65535")
	}
	gpus := "all"
	if value, present := raw["gpus"]; present && value != nil {
		gpus = strings.ToLower(strings.ReplaceAll(text(value), " ", ""))
		if gpus == "" {
			gpus = "none"
		}
	}
	if !gpusPattern.MatchString(gpus) {
		return Spec{}, errors.New("GPUs must be all, none, or card numbers such as 0 or 0,1")
	}
	allowed := map[string]bool{}
	names := []string{}
	for _, option := range engine.Options {
		allowed[option.Env] = true
		names = append(names, option.Env)
	}
	sort.Strings(names)
	env := map[string]string{}
	if values, ok := raw["env"].(map[string]any); ok {
		for key, value := range values {
			if !allowed[key] {
				return Spec{}, fmt.Errorf("%s isn't a %s setting; use one of: %s", key, engine.Label, strings.Join(names, ", "))
			}
			setting := ""
			if value != nil {
				setting = strings.TrimSpace(text(value))
			}
			if !envPattern.MatchString(setting) {
				return Spec{}, fmt.Errorf("%s has an invalid value", key)
			}
			if setting != "" {
				env[key] = setting
			}
		}
	} else if raw["env"] != nil {
		return Spec{}, errors.New("env must be an object")
	}
	spec := Spec{Engine: engine.ID, Image: image, Port: port, GPUs: gpus, Env: env}
	if volume := strings.TrimSpace(text(raw["volume"])); volume != "" {
		if !volumePattern.MatchString(volume) && !strings.HasPrefix(volume, "/") && !filepath.IsAbs(volume) && !drivePattern.MatchString(volume) {
			return Spec{}, errors.New("Volume must be a volume name such as strata-data or an absolute folder")
		}
		if strings.ContainsAny(volume, ",\n") {
			return Spec{}, errors.New("Volume can't contain commas or line breaks")
		}
		spec.Volume = volume
	}
	return spec, nil
}

// text is a JSON value as Python's str() would write it, for the values a spec holds.
func text(value any) string {
	switch typed := value.(type) {
	case nil:
		return ""
	case string:
		return typed
	case float64:
		return strconv.FormatFloat(typed, 'f', -1, 64)
	case bool:
		if typed {
			return "True"
		}
		return "False"
	default:
		return fmt.Sprint(typed)
	}
}

// CheckName refuses names that aren't the server's backend names.
func CheckName(name string) error {
	if !namePattern.MatchString(name) {
		return fmt.Errorf("%q isn't a container name (letters, numbers, - and _, up to 40)", name)
	}
	return nil
}

func checkKey(key string) error {
	if !keyPattern.MatchString(key) {
		return errors.New("the API key has characters an API key can't have")
	}
	return nil
}

func checkBind(bind string) (string, error) {
	switch bind {
	case "", "127.0.0.1":
		return "127.0.0.1", nil
	case "0.0.0.0":
		return bind, nil
	}
	return "", fmt.Errorf("%q isn't an address containers listen on (127.0.0.1 or 0.0.0.0)", bind)
}

// RunArgs is the `docker run` for a container, and the digest of it that marks the
// container, so a changed spec shows as outdated. The digest is the one the server's
// Python computed before containers moved to machines, so existing containers aren't
// replaced for no reason.
func RunArgs(name string, spec Spec, key, bind string) ([]string, string) {
	return RunArgsFor("docker", name, spec, key, bind)
}

// RunArgsFor is the run command for a CLI. Podman takes the same arguments as Docker but
// GPUs: it hands them over as CDI devices (the NVIDIA Container Toolkit's nvidia.com/gpu),
// where Docker has --gpus.
func RunArgsFor(cli, name string, spec Spec, key, bind string) ([]string, string) {
	engine := EngineByID(spec.Engine)
	env := map[string]string{}
	for key, value := range spec.Env {
		env[key] = value
	}
	if engine.ID == "strata" {
		// Its own preset inside the shared /data keeps settings, tuning and key per container.
		env["PRESET"] = name
		env["API_KEY"] = key
	}
	volume := spec.Volume
	if volume == "" {
		volume = engine.Volume
	}
	args := []string{
		"run", "-d", "--name", Prefix + name,
		"--label", label + "=" + name, "--label", "ai-proxy.engine=" + engine.ID,
		"-p", fmt.Sprintf("%s:%d:%d", bind, spec.Port, engine.Port), "-v", volume + ":" + engine.DataPath,
		"--stop-timeout", strconv.Itoa(engine.StopTimeout),
	}
	args = append(args, engine.Flags...)
	switch {
	case cli == "podman" && spec.GPUs != "none":
		for _, card := range strings.Split(spec.GPUs, ",") {
			args = append(args, "--device", "nvidia.com/gpu="+card)
		}
	case spec.GPUs == "all":
		args = append(args, "--gpus", "all")
	case spec.GPUs != "none" && strings.Contains(spec.GPUs, ","):
		// Docker reads --gpus as CSV, so a list of cards needs the quotes.
		args = append(args, "--gpus", `"device=`+spec.GPUs+`"`)
	case spec.GPUs != "none":
		args = append(args, "--gpus", "device="+spec.GPUs)
	}
	keys := make([]string, 0, len(env))
	for key := range env {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		args = append(args, "-e", key+"="+env[key])
	}
	sum := sha256.Sum256([]byte(pythonJSON(append(append([]string{}, args...), spec.Image))))
	digest := hex.EncodeToString(sum[:])[:16]
	return append(args, "--label", "ai-proxy.spec="+digest, spec.Image), digest
}

// CommandLine is the run command to show, quoted for a shell, with the API key hidden.
func CommandLine(cli string, args []string, key string) string {
	shown := make([]string, 0, len(args)+1)
	shown = append(shown, shellQuote(cli))
	for _, arg := range args {
		if key != "" {
			arg = strings.ReplaceAll(arg, key, "<api key>")
		}
		shown = append(shown, shellQuote(arg))
	}
	return strings.Join(shown, " ")
}

// pythonJSON writes a list of strings exactly as Python's json.dumps does by default:
// ", " between items and every character outside printable ASCII escaped.
func pythonJSON(values []string) string {
	var builder strings.Builder
	builder.WriteByte('[')
	for index, value := range values {
		if index > 0 {
			builder.WriteString(", ")
		}
		builder.WriteByte('"')
		for _, r := range value {
			switch r {
			case '"':
				builder.WriteString(`\"`)
			case '\\':
				builder.WriteString(`\\`)
			case '\n':
				builder.WriteString(`\n`)
			case '\r':
				builder.WriteString(`\r`)
			case '\t':
				builder.WriteString(`\t`)
			case '\b':
				builder.WriteString(`\b`)
			case '\f':
				builder.WriteString(`\f`)
			default:
				switch {
				case r >= 0x20 && r <= 0x7e:
					builder.WriteRune(r)
				case r > 0xffff:
					r -= 0x10000
					fmt.Fprintf(&builder, `\u%04x\u%04x`, 0xd800+(r>>10), 0xdc00+(r&0x3ff))
				default:
					fmt.Fprintf(&builder, `\u%04x`, r)
				}
			}
		}
		builder.WriteByte('"')
	}
	builder.WriteByte(']')
	return builder.String()
}

var safeShell = regexp.MustCompile(`^[A-Za-z0-9_@%+=:,./-]+$`)

// shellQuote quotes like Python's shlex.quote.
func shellQuote(value string) string {
	if value == "" {
		return "''"
	}
	if safeShell.MatchString(value) {
		return value
	}
	return "'" + strings.ReplaceAll(value, "'", `'"'"'`) + "'"
}
