package presence

import (
	"os"
	"os/exec"

	"ai-coding-agent-proxy/machine/internal/process"
	"ai-coding-agent-proxy/machine/internal/settings"
	"ai-coding-agent-proxy/machine/internal/update"
)

// UpdateRequest is the server's {"type": "update"}: update agent-machine here (and with Agent, the
// agent command too) to the server's own build, and restart on it.
type UpdateRequest struct {
	Type  string `json:"type"`
	ID    string `json:"request"`
	Op    string `json:"op"`
	Agent bool   `json:"agent"`
}

// RemoteUpdate answers an update request, as the update_result to send back. It only ever installs
// the build the server offers, checked against its SHA-256, and only while the machine allows it
// (agent-machine updates on). The work is `agent-machine update` run detached, so it can stop
// this process and start the new binary; its output goes to agent-machine's log.
func (p *Presence) RemoteUpdate(server string, request UpdateRequest) map[string]any {
	answer := map[string]any{"type": "update_result", "request": request.ID}
	refuse := func(message string) map[string]any {
		answer["ok"], answer["error"] = false, message
		return answer
	}
	if !settings.LoadMachine().Updates {
		return refuse("this machine doesn't take updates from the server (agent-machine updates on)")
	}
	index, err := update.Fetch(server)
	if err != nil {
		return refuse(err.Error())
	}
	answer["ok"], answer["from"], answer["to"] = true, settings.Version, index.Version
	if index.Version == settings.Version && !request.Agent {
		answer["updating"], answer["message"] = false, "agent-machine is already the server's build"
		return answer
	}
	args := []string{"update", "--server", server}
	if request.Agent {
		args = append(args, "--with-agent")
	}
	spawn := p.SpawnUpdate
	if spawn == nil {
		spawn = spawnUpdate
	}
	if err := spawn(args); err != nil {
		return refuse("couldn't start the update: " + err.Error())
	}
	p.logf("fleet: updating to %s, as the server asked (agent too: %v)", index.Version, request.Agent)
	answer["updating"] = true
	return answer
}

// spawnUpdate runs this binary with args, detached from this process so it outlives it, writing to the log.
func spawnUpdate(args []string) error {
	self, err := os.Executable()
	if err != nil {
		return err
	}
	cmd := exec.Command(self, args...)
	process.Detach(cmd)
	if log, err := os.OpenFile(settings.LogPath(), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600); err == nil {
		cmd.Stdout, cmd.Stderr = log, log
		defer log.Close()
	}
	if err := cmd.Start(); err != nil {
		return err
	}
	return cmd.Process.Release()
}
