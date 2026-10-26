package containers

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"ai-coding-agent-proxy/machine/internal/settings"
)

func TestSpecsAcceptOnlyKnownEnginesSettingsAndPlainImages(t *testing.T) {
	spec, err := Validate(map[string]any{"engine": "strata", "env": map[string]any{"MODEL": "IQ3_XXS", "CONTEXT": float64(32768), "VISION": ""}})
	want := Spec{Engine: "strata", Image: "ghcr.io/niko1221/strata:latest-cuda13", Port: 8080, GPUs: "all", Env: map[string]string{"MODEL": "IQ3_XXS", "CONTEXT": "32768"}}
	if err != nil || !reflect.DeepEqual(spec, want) {
		t.Fatalf("%+v %v", spec, err)
	}
	for _, bad := range []struct {
		raw     map[string]any
		message string
	}{
		{map[string]any{"engine": "vllm"}, "Unknown engine"},
		{map[string]any{"engine": "strata", "image": "--privileged"}, "Image"},
		{map[string]any{"engine": "strata", "env": map[string]any{"LD_PRELOAD": "x"}}, "isn't a Strata setting"},
		{map[string]any{"engine": "strata", "env": map[string]any{"MODEL": "a\nb"}}, "invalid value"},
		{map[string]any{"engine": "strata", "gpus": "0;rm"}, "GPUs"},
		{map[string]any{"engine": "strata", "port": float64(80)}, "Port"},
		{map[string]any{"engine": "ollama", "volume": "../etc"}, "Volume"},
		{map[string]any{"engine": "ollama", "volume": "/data,readonly"}, "commas"},
	} {
		if _, err := Validate(bad.raw); err == nil || !strings.Contains(err.Error(), bad.message) {
			t.Errorf("%v: %v", bad.raw, err)
		}
	}
	if spec, err := Validate(map[string]any{"engine": "ollama", "volume": `D:\models`, "gpus": "0, 1", "port": "11500"}); err != nil || spec.Volume != `D:\models` || spec.GPUs != "0,1" || spec.Port != 11500 {
		t.Fatalf("%+v %v", spec, err)
	}
	for _, name := range []string{"", "-x", "a b", strings.Repeat("a", 41), "../x"} {
		if CheckName(name) == nil {
			t.Errorf("name %q accepted", name)
		}
	}
	if checkKey("abc$(x)") == nil {
		t.Error("key with shell characters accepted")
	}
	if _, err := checkBind("192.168.1.2"); err == nil {
		t.Error("bind to another address accepted")
	}
}

// The digests were computed by the server's Python run_args before containers moved to
// machines; they have to match, or every existing container would show as outdated.
func TestRunArgsFollowTheEngineTemplatesAndPythonsDigest(t *testing.T) {
	spec, err := Validate(map[string]any{"engine": "strata", "port": float64(8081), "gpus": "0,1", "env": map[string]any{"MODEL": "IQ3_XXS", "KV": "q4_0"}})
	if err != nil {
		t.Fatal(err)
	}
	args, digest := RunArgs("strata-iq3", spec, "k123", "127.0.0.1")
	want := []string{"run", "-d", "--name", "ai-proxy-strata-iq3", "--label", "ai-proxy.backend=strata-iq3", "--label", "ai-proxy.engine=strata",
		"-p", "127.0.0.1:8081:8080", "-v", "strata-data:/data", "--stop-timeout", "120", "--ulimit", "memlock=-1:-1", "--shm-size", "2g",
		"--gpus", `"device=0,1"`, "-e", "API_KEY=k123", "-e", "KV=q4_0", "-e", "MODEL=IQ3_XXS", "-e", "PRESET=strata-iq3",
		"--label", "ai-proxy.spec=67833d78f3a7389c", "ghcr.io/niko1221/strata:latest-cuda13"}
	if digest != "67833d78f3a7389c" || !reflect.DeepEqual(args, want) {
		t.Fatalf("%s\n%q", digest, args)
	}
	shown := CommandLine("docker", args, "k123")
	if !strings.Contains(shown, `--gpus '"device=0,1"' -e 'API_KEY=<api key>' -e KV=q4_0`) || strings.Contains(shown, "k123") {
		t.Fatalf("command line %s", shown)
	}
	// Escaping outside printable ASCII, the way json.dumps does it.
	other := Spec{Engine: "ollama", Image: "ollama/ollama:latest", Port: 11435, GPUs: "none", Env: map[string]string{"OLLAMA_KV_CACHE_TYPE": "q8_0", "X": "café \"q\" <&>"}}
	args, digest = RunArgs("ollama-box", other, "", "0.0.0.0")
	joined := strings.Join(args, " ")
	if digest != "43c74135e0d20aed" || strings.Contains(joined, "--gpus") || strings.Contains(joined, "PRESET") || !strings.Contains(joined, "-v ollama-models:/root/.ollama") {
		t.Fatalf("%s %s", digest, joined)
	}
	changed, _ := Validate(map[string]any{"engine": "strata", "port": float64(8081), "gpus": "0,1", "env": map[string]any{"MODEL": "IQ3_S"}})
	if _, again := RunArgs("strata-iq3", changed, "k123", "127.0.0.1"); again == "67833d78f3a7389c" {
		t.Fatal("a changed spec kept its digest")
	}
}

func TestEverythingIsRefusedUntilTheMachineAllowsContainers(t *testing.T) {
	manager := &Manager{Machine: func() *settings.Machine { return &settings.Machine{} }}
	result := manager.Handle(Request{ID: "r1", Op: "status"})
	if result["ok"] != false || result["type"] != "container_result" || result["request"] != "r1" || !strings.Contains(result["error"].(string), "agent-machine containers on") {
		t.Fatalf("%v", result)
	}
	if described := manager.Describe(); described["enabled"] != false || len(described["engines"].([]Engine)) != 0 {
		t.Fatalf("%v", described)
	}
}

// Drives the server's own fake docker (server/tests/fake_docker.py) through a start, a
// switch and a stop, as the server's console would.
func TestJobsStartSwitchAndStopContainersWithTheCLI(t *testing.T) {
	python, err := exec.LookPath("python")
	if err != nil {
		if python, err = exec.LookPath("python3"); err != nil {
			t.Skip("python isn't installed")
		}
	}
	fake, _ := filepath.Abs(filepath.Join("..", "..", "..", "server", "tests", "fake_docker.py"))
	if _, err := os.Stat(fake); err != nil {
		t.Skip("the server's fake docker isn't here")
	}
	state := filepath.Join(t.TempDir(), "docker.json")
	command, _ := json.Marshal([]string{python, fake})
	t.Setenv(CommandEnv, string(command))
	t.Setenv("FAKE_DOCKER_STATE", state)
	manager := &Manager{Machine: func() *settings.Machine { return &settings.Machine{Containers: true} }}
	described := manager.Describe()
	if described["online"] != true || described["version"] != "27.3.1" || len(described["engines"].([]Engine)) != 2 {
		t.Fatalf("describe %v", described)
	}
	spec := map[string]any{"engine": "strata", "port": float64(18080), "env": map[string]any{"MODEL": "IQ3_XXS"}}
	wait := func(name string) map[string]any {
		deadline := time.Now().Add(30 * time.Second)
		for time.Now().Before(deadline) {
			result := manager.Handle(Request{Op: "status", Containers: map[string]Target{name: {Spec: spec, Key: "k1"}}})
			item := result["containers"].(map[string]any)[name].(map[string]any)
			if job, _ := item["job"].(map[string]any); job != nil && job["state"] != "running" {
				return item
			}
			time.Sleep(50 * time.Millisecond)
		}
		t.Fatalf("%s didn't finish", name)
		return nil
	}

	started := manager.Handle(Request{Op: "start", Name: "s1", Spec: spec, Key: "k1"})
	if started["ok"] != true {
		t.Fatalf("start %v", started)
	}
	item := wait("s1")
	job := item["job"].(map[string]any)
	if job["state"] != "done" || item["outdated"] != false || !strings.Contains(item["command"].(string), "<api key>") {
		t.Fatalf("after start %v", item)
	}
	if lines := strings.Join(job["lines"].([]string), "|"); !strings.Contains(lines, "Pulling ghcr.io/niko1221/strata:latest-cuda13") || !strings.Contains(lines, "Creating the container") {
		t.Fatalf("lines %s", lines)
	}
	if state := item["state"].(State); !state.Running || state.Health != "starting" {
		t.Fatalf("state %+v", state)
	}
	logs := manager.Handle(Request{Op: "logs", Name: "s1", Key: "k1"})
	if text, _ := logs["text"].(string); !strings.Contains(text, "<api key>") || strings.Contains(text, "k1") {
		t.Fatalf("logs %v", logs)
	}

	other := map[string]any{"engine": "strata", "port": float64(18081)}
	switched := manager.Handle(Request{Op: "start", Name: "s2", Spec: other, StopFirst: []Other{{Name: "s1", Engine: "strata"}}})
	if switched["ok"] != true || switched["job"].(map[string]any)["action"] != "switch" {
		t.Fatalf("switch %v", switched)
	}
	spec = other
	if item := wait("s2"); item["job"].(map[string]any)["state"] != "done" {
		t.Fatalf("after switch %v", item)
	}
	var docker struct {
		Calls      [][]string                `json:"calls"`
		Containers map[string]map[string]any `json:"containers"`
	}
	data, _ := os.ReadFile(state)
	_ = json.Unmarshal(data, &docker)
	if docker.Containers["ai-proxy-s1"]["running"] != false || docker.Containers["ai-proxy-s2"]["running"] != true {
		t.Fatalf("containers %v", docker.Containers)
	}
	pulls := 0
	for _, call := range docker.Calls {
		if call[0] == "pull" {
			pulls++
		}
	}
	if pulls != 1 || !containsCall(docker.Calls, []string{"stop", "-t", "120", "ai-proxy-s1"}) {
		t.Fatalf("calls %v", docker.Calls)
	}
	if removed := manager.Handle(Request{Op: "remove", Name: "s1", Engine: "strata"}); removed["ok"] != true {
		t.Fatalf("remove %v", removed)
	}
	if refused := manager.Handle(Request{Op: "start", Name: "s3", Spec: map[string]any{"engine": "strata", "image": "--privileged"}}); refused["ok"] != false {
		t.Fatalf("bad spec %v", refused)
	}
}

func TestOneJobPerContainerAtATime(t *testing.T) {
	manager := &Manager{}
	release := make(chan struct{})
	if _, err := manager.run("s1", "start", func(*Job) error { <-release; return nil }); err != nil {
		t.Fatal(err)
	}
	if _, err := manager.run("s1", "stop", func(*Job) error { return nil }); err == nil || !strings.Contains(err.Error(), "s1 is busy with start") {
		t.Fatalf("second job: %v", err)
	}
	if _, err := manager.run("s2", "stop", func(*Job) error { return nil }); err != nil {
		t.Fatalf("another container: %v", err)
	}
	close(release)
}

func containsCall(calls [][]string, want []string) bool {
	for _, call := range calls {
		if reflect.DeepEqual(call, want) {
			return true
		}
	}
	return false
}

func TestPodmanGetsTheGPUsAsCDIDevices(t *testing.T) {
	spec := Spec{Engine: "ollama", Image: "ollama/ollama:latest", Port: 11500, GPUs: "0,1"}
	args, _ := RunArgsFor("podman", "box", spec, "", "0.0.0.0")
	joined := strings.Join(args, " ")
	if !strings.Contains(joined, "--device nvidia.com/gpu=0 --device nvidia.com/gpu=1") || strings.Contains(joined, "--gpus") {
		t.Fatalf("%s", joined)
	}
	spec.GPUs = "all"
	if args, _ := RunArgsFor("podman", "box", spec, "", "0.0.0.0"); !strings.Contains(strings.Join(args, " "), "--device nvidia.com/gpu=all") {
		t.Fatalf("%v", args)
	}
	spec.GPUs = "none"
	if args, _ := RunArgsFor("podman", "box", spec, "", "0.0.0.0"); strings.Contains(strings.Join(args, " "), "gpu") {
		t.Fatalf("%v", args)
	}
	// Docker's arguments, and so existing containers' digests, are unchanged.
	if docker, _ := RunArgsFor("docker", "box", spec, "", "0.0.0.0"); strings.Join(docker, " ") != strings.Join(mustRunArgs(spec), " ") {
		t.Fatal("RunArgs isn't docker's")
	}
}

func mustRunArgs(spec Spec) []string {
	args, _ := RunArgs("box", spec, "", "0.0.0.0")
	return args
}

func TestTheCLIThatAnswersIsChosen(t *testing.T) {
	python, err := exec.LookPath("python")
	if err != nil {
		t.Skip("Python isn't installed")
	}
	script := func(code string) []string { return []string{python, "-c", code} }
	// Docker installed but its engine down; Podman on Linux, answering without a Server part.
	dockerDown := CLI{Name: "docker", Program: script(`import sys; print('{"Client": {"Version": "27"}, "Server": null}'); sys.exit(1)`)}
	podmanUp := CLI{Name: "podman", Program: script(`print('{"Client": {"Version": "5.2.0"}}')`)}
	if chosen := pick(dockerDown, podmanUp); chosen.Name != "podman" {
		t.Fatalf("picked %s", chosen.Name)
	}
	if runtime := podmanUp.Probe(); !runtime.Online || runtime.Version != "5.2.0" {
		t.Fatalf("podman probe %+v", runtime)
	}
	dockerUp := CLI{Name: "docker", Program: script(`print('{"Client": {"Version": "27"}, "Server": {"Version": "27.3"}}')`)}
	if chosen := pick(dockerUp, podmanUp); chosen.Name != "docker" {
		t.Fatalf("picked %s with docker answering", chosen.Name)
	}
	// Neither answers: the installed one, so the console says why it isn't answering.
	if chosen := pick(dockerDown, CLI{Name: "podman", Program: script(`import sys; sys.exit(125)`)}); chosen.Name != "docker" {
		t.Fatalf("picked %s with nothing answering", chosen.Name)
	}
}
