package containers

import (
	"sync"
	"time"

	"ai-coding-agent-proxy/machine/internal/settings"
)

// detectFor is how long a detection stands: requests come every few seconds while the
// console's Containers page is open, and each probe runs the CLI.
const detectFor = 30 * time.Second

var detected struct {
	sync.Mutex
	at  time.Time
	cli CLI
}

// Detect is the container CLI installed here when the machine doesn't name one: Docker
// when it's installed and its engine answers, else Podman when it does, else whichever is
// installed (Docker first), so the console can say why it isn't answering.
func Detect() CLI {
	detected.Lock()
	defer detected.Unlock()
	if !detected.at.IsZero() && time.Since(detected.at) < detectFor {
		return detected.cli
	}
	detected.cli, detected.at = pick(NewCLI("docker"), NewCLI("podman")), time.Now()
	return detected.cli
}

func pick(candidates ...CLI) CLI {
	var installed []CLI
	for _, cli := range candidates {
		if cli.Command() == nil {
			continue
		}
		if cli.Probe().Online {
			return cli
		}
		installed = append(installed, cli)
	}
	if len(installed) > 0 {
		return installed[0]
	}
	return candidates[0]
}

// OnPath is the CLI the machine names, or the first of docker and podman on PATH, without
// running either: for a machine that hasn't turned containers on.
func OnPath(machine *settings.Machine) CLI {
	if machine.ContainerCLI == "docker" || machine.ContainerCLI == "podman" {
		return NewCLI(machine.ContainerCLI)
	}
	for _, name := range []string{"docker", "podman"} {
		if cli := NewCLI(name); cli.Command() != nil {
			return cli
		}
	}
	return NewCLI("docker")
}

// forgetDetection makes the next Detect look again (tests, and `containers cli` changes).
func forgetDetection() {
	detected.Lock()
	detected.at = time.Time{}
	detected.Unlock()
}
