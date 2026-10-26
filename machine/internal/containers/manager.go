package containers

import (
	"errors"
	"fmt"
	"strconv"
	"strings"
	"sync"
	"time"

	"ai-coding-agent-proxy/machine/internal/settings"
)

const (
	jobLines   = 200
	shownLines = 30
)

// Job is the latest start, stop or switch of one container, shown while it runs and after.
type Job struct {
	mu         sync.Mutex
	action     string
	state      string
	startedAt  float64
	finishedAt float64
	err        string
	lines      []string
}

func (j *Job) say(line string) {
	j.mu.Lock()
	defer j.mu.Unlock()
	j.lines = append(j.lines, line)
	if len(j.lines) > jobLines {
		j.lines = j.lines[len(j.lines)-jobLines:]
	}
}

// Public is the job for the server: its last lines only.
func (j *Job) Public() map[string]any {
	j.mu.Lock()
	defer j.mu.Unlock()
	lines := j.lines
	if len(lines) > shownLines {
		lines = lines[len(lines)-shownLines:]
	}
	value := map[string]any{
		"action": j.action, "state": j.state, "started_at": j.startedAt, "finished_at": nil, "error": nil,
		"lines": append([]string{}, lines...),
	}
	if j.finishedAt != 0 {
		value["finished_at"] = j.finishedAt
	}
	if j.err != "" {
		value["error"] = j.err
	}
	return value
}

func (j *Job) running() bool {
	j.mu.Lock()
	defer j.mu.Unlock()
	return j.state == "running"
}

// Target is one container in a request: its spec, Strata key and listen address.
type Target struct {
	Spec map[string]any `json:"spec"`
	Key  string         `json:"key"`
	Bind string         `json:"bind"`
}

// Request is the server's {"type": "container"} message.
type Request struct {
	Type       string            `json:"type"`
	ID         string            `json:"request"`
	Op         string            `json:"op"`
	Name       string            `json:"name"`
	Spec       map[string]any    `json:"spec"`
	Key        string            `json:"key"`
	Bind       string            `json:"bind"`
	Engine     string            `json:"engine"`
	Tail       int               `json:"tail"`
	StopFirst  []Other           `json:"stop_first"`
	Containers map[string]Target `json:"containers"`
}

// Other is a container to stop before a switch.
type Other struct {
	Name   string `json:"name"`
	Engine string `json:"engine"`
}

// Manager answers the server's container requests and keeps the jobs they start.
type Manager struct {
	// Machine reads this machine's settings, fresh for every request so `agent-machine
	// containers off` applies at once; tests replace it.
	Machine func() *settings.Machine

	mu   sync.Mutex
	jobs map[string]*Job
	// The ports of the containers the server has named, which its tunnels may reach (presence/tunnel.go).
	ports map[int]bool
}

// Ports are the ports of the containers the server has asked about: where they serve on this machine.
func (m *Manager) Ports() []int {
	m.mu.Lock()
	defer m.mu.Unlock()
	ports := make([]int, 0, len(m.ports))
	for port := range m.ports {
		ports = append(ports, port)
	}
	return ports
}

// remember keeps a container spec's port, from any request that carries one.
func (m *Manager) remember(spec map[string]any) {
	port, ok := spec["port"].(float64)
	if !ok || port < 1 || port > 65535 {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.ports == nil {
		m.ports = map[int]bool{}
	}
	m.ports[int(port)] = true
}

func (m *Manager) machine() *settings.Machine {
	if m.Machine != nil {
		return m.Machine()
	}
	return settings.LoadMachine()
}

// CLIFor is the CLI the machine's settings name, or the one installed here when they
// don't name one (see Detect).
func CLIFor(machine *settings.Machine) CLI {
	if machine.ContainerCLI == "docker" || machine.ContainerCLI == "podman" {
		return NewCLI(machine.ContainerCLI)
	}
	return Detect()
}

// Describe is what the hello says about containers: whether they're on, the CLI, and the engines.
func (m *Manager) Describe() map[string]any {
	machine := m.machine()
	if !machine.Containers {
		// Nothing runs until the owner opts in; finding the CLI only looks at PATH.
		cli := OnPath(machine)
		return map[string]any{"enabled": false, "cli": cli.Name, "found": cli.Command() != nil, "online": false, "version": "", "error": "", "engines": []Engine{}}
	}
	cli := CLIFor(machine)
	runtime := cli.Probe()
	return map[string]any{
		"enabled": true, "cli": cli.Name, "found": runtime.Found, "online": runtime.Online,
		"version": runtime.Version, "error": runtime.Error, "engines": Engines,
	}
}

// Handle answers one request, as the container_result to send back.
func (m *Manager) Handle(request Request) map[string]any {
	if m.machine().Containers {
		m.remember(request.Spec)
		for _, target := range request.Containers {
			m.remember(target.Spec)
		}
	}
	result, err := m.handle(request)
	if err != nil {
		result = map[string]any{"ok": false, "error": err.Error()}
	} else {
		result["ok"] = true
	}
	result["type"] = "container_result"
	result["request"] = request.ID
	return result
}

func (m *Manager) handle(request Request) (map[string]any, error) {
	machine := m.machine()
	if !machine.Containers {
		return nil, errors.New("Containers are off on this machine (agent-machine containers on)")
	}
	cli := CLIFor(machine)
	if request.Op == "status" {
		return m.status(cli, request.Containers), nil
	}
	if err := CheckName(request.Name); err != nil {
		return nil, err
	}
	switch request.Op {
	case "start", "restart":
		spec, args, err := prepare(cli, request.Name, Target{Spec: request.Spec, Key: request.Key, Bind: request.Bind})
		if err != nil {
			return nil, err
		}
		others := []Other{}
		if request.Op == "start" {
			for _, other := range request.StopFirst {
				if err := CheckName(other.Name); err != nil {
					return nil, err
				}
				others = append(others, other)
			}
		}
		action := request.Op
		if len(others) > 0 {
			action = "switch"
		}
		job, err := m.run(request.Name, action, func(job *Job) error {
			if request.Op == "restart" {
				if err := stop(cli, request.Name, spec.Engine, job); err != nil {
					return err
				}
			}
			return start(cli, request.Name, spec, args, others, job)
		})
		if err != nil {
			return nil, err
		}
		return map[string]any{"job": job.Public()}, nil
	case "stop":
		job, err := m.run(request.Name, "stop", func(job *Job) error { return stop(cli, request.Name, request.Engine, job) })
		if err != nil {
			return nil, err
		}
		return map[string]any{"job": job.Public()}, nil
	case "remove":
		code, _, errText := cli.Run(time.Duration(stopTimeout(request.Engine)+60)*time.Second, "rm", "-f", Prefix+request.Name)
		if code != 0 && !strings.Contains(strings.ToLower(errText), "no such") {
			return nil, errors.New(orDefault(strings.TrimSpace(errText), cli.Name+" rm failed"))
		}
		m.mu.Lock()
		delete(m.jobs, request.Name)
		m.mu.Unlock()
		return map[string]any{}, nil
	case "logs":
		if err := checkKey(request.Key); err != nil {
			return nil, err
		}
		tail := min(max(request.Tail, 10), 5000)
		if request.Tail == 0 {
			tail = 400
		}
		text := cli.Combined(20*time.Second, "logs", "--tail", strconv.Itoa(tail), Prefix+request.Name)
		if request.Key != "" {
			text = strings.ReplaceAll(text, request.Key, "<api key>")
		}
		return map[string]any{"text": text}, nil
	}
	return nil, fmt.Errorf("unknown container request %q", request.Op)
}

// prepare checks a container's spec, key and address, and builds its run arguments.
func prepare(cli CLI, name string, target Target) (Spec, []string, error) {
	spec, err := Validate(target.Spec)
	if err != nil {
		return Spec{}, nil, err
	}
	if err := checkKey(target.Key); err != nil {
		return Spec{}, nil, err
	}
	bind, err := checkBind(target.Bind)
	if err != nil {
		return Spec{}, nil, err
	}
	args, _ := RunArgsFor(cli.Name, name, spec, target.Key, bind)
	return spec, args, nil
}

func (m *Manager) status(cli CLI, targets map[string]Target) map[string]any {
	names := []string{}
	for name := range targets {
		if CheckName(name) == nil {
			names = append(names, name)
		}
	}
	states := cli.Inspect(names)
	items := map[string]any{}
	for name, target := range targets {
		item := map[string]any{"state": nil, "job": nil, "outdated": false, "command": nil, "problem": nil}
		if err := CheckName(name); err != nil {
			item["problem"] = err.Error()
			items[name] = item
			continue
		}
		state, exists := states[name]
		if exists {
			item["state"] = state
		}
		if job := m.job(name); job != nil {
			item["job"] = job.Public()
		}
		if _, args, err := prepare(cli, name, target); err != nil {
			item["problem"] = err.Error()
		} else {
			digest := strings.TrimPrefix(args[len(args)-2], "ai-proxy.spec=")
			item["outdated"] = exists && state.Spec != digest
			item["command"] = CommandLine(cli.Name, args, target.Key)
		}
		items[name] = item
	}
	return map[string]any{"runtime": cli.Probe(), "containers": items}
}

func (m *Manager) job(name string) *Job {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.jobs[name]
}

// run starts work as the container's job, unless one is already running for it.
func (m *Manager) run(name, action string, work func(*Job) error) (*Job, error) {
	m.mu.Lock()
	if current := m.jobs[name]; current != nil && current.running() {
		m.mu.Unlock()
		return nil, fmt.Errorf("%s is busy with %s; wait for it to finish", name, current.action)
	}
	if m.jobs == nil {
		m.jobs = map[string]*Job{}
	}
	job := &Job{action: action, state: "running", startedAt: now()}
	m.jobs[name] = job
	m.mu.Unlock()
	go func() {
		err := work(job)
		if err != nil {
			job.say("Failed: " + err.Error())
		}
		job.mu.Lock()
		job.finishedAt = now()
		if err != nil {
			job.state, job.err = "failed", err.Error()
		} else {
			job.state = "done"
		}
		job.mu.Unlock()
	}()
	return job, nil
}

func start(cli CLI, name string, spec Spec, args []string, others []Other, job *Job) error {
	for _, other := range others {
		job.say("Stopping " + other.Name + " to free the GPU")
		if err := stop(cli, other.Name, other.Engine, job); err != nil {
			return err
		}
	}
	digest := strings.TrimPrefix(args[len(args)-2], "ai-proxy.spec=")
	image := args[len(args)-1]
	current, exists := cli.Inspect([]string{name})[name]
	if exists && current.Spec == digest {
		if current.Running {
			job.say("Already running")
			return nil
		}
		job.say("Starting the container")
		if code, _, errText := cli.Run(120*time.Second, "start", Prefix+name); code != 0 {
			return errors.New(orDefault(strings.TrimSpace(errText), "start failed"))
		}
		job.say("Started")
		return nil
	}
	if exists {
		job.say("Its settings changed: replacing the old container")
		cli.Run(time.Duration(stopTimeout(spec.Engine)+60)*time.Second, "rm", "-f", Prefix+name)
	}
	if code, _, _ := cli.Run(30*time.Second, "image", "inspect", image); code != 0 {
		job.say("Pulling " + image)
		if cli.Stream(time.Hour, job.say, "pull", image) != 0 {
			return fmt.Errorf("Couldn't pull %s", image)
		}
	}
	job.say("Creating the container")
	if code, _, errText := cli.Run(180*time.Second, args...); code != 0 {
		return errors.New(orDefault(strings.TrimSpace(errText), "run failed"))
	}
	if spec.Engine == "strata" {
		job.say("Started. Strata downloads its model on the first start, then loads it.")
	} else {
		job.say("Started")
	}
	return nil
}

func stop(cli CLI, name, engine string, job *Job) error {
	timeout := stopTimeout(engine)
	job.say(fmt.Sprintf("Stopping %s (up to %d s while it unloads)", name, timeout))
	code, _, errText := cli.Run(time.Duration(timeout+60)*time.Second, "stop", "-t", strconv.Itoa(timeout), Prefix+name)
	if code != 0 && !strings.Contains(strings.ToLower(errText), "no such") {
		return errors.New(orDefault(strings.TrimSpace(errText), "stop failed"))
	}
	return nil
}

func orDefault(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}

func now() float64 {
	return float64(time.Now().UnixNano()) / 1e9
}
