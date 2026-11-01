package presence

import (
	"context"
	"encoding/json"
	"os/exec"
	"strings"
	"sync"
	"time"
)

// How often the provider CLIs here are looked at again, and how long one look may take.
const (
	accountsEvery   = 5 * time.Minute
	accountsTimeout = 60 * time.Second
)

// providerCLIs is what `agent accounts detect --json` last found here: which provider CLIs
// (claude, codex, copilot) are installed and whether each is signed in, for the hello's
// "accounts". It's metadata only: the agent command never reads a credential to find it, and
// nothing secret is in it. Looking runs in the background, so a hello never waits for it.
type providerCLIs struct {
	mu      sync.Mutex
	report  map[string]any
	checked time.Time
	running bool
	// detect runs the look; tests replace it.
	detect func() map[string]any
	// changed hears each look that found something new, so an update goes out then, not a minute later.
	changed chan struct{}
}

// Changed is told about each look that found something new.
func (c *providerCLIs) Changed() <-chan struct{} {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.changed == nil {
		c.changed = make(chan struct{}, 1)
	}
	return c.changed
}

// Report is the last look, starting a new one when it's due. Before the first one finishes it's
// an empty report, which still tells the server this agent-machine can report provider sign-ins.
func (c *providerCLIs) Report() map[string]any {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.running && time.Since(c.checked) > accountsEvery {
		c.running = true
		go func() {
			detect := c.detect
			if detect == nil {
				detect = detectAccounts
			}
			report := detect()
			c.mu.Lock()
			old, _ := json.Marshal(c.report)
			found, _ := json.Marshal(report)
			c.report, c.checked, c.running = report, time.Now(), false
			if c.changed == nil {
				c.changed = make(chan struct{}, 1)
			}
			if string(old) != string(found) {
				select {
				case c.changed <- struct{}{}:
				default:
				}
			}
			c.mu.Unlock()
		}()
	}
	if c.report == nil {
		return map[string]any{"clis": []any{}}
	}
	return c.report
}

// detectAccounts runs this computer's agent command, which knows the provider CLIs.
func detectAccounts() map[string]any {
	program, err := AgentCommand()
	if err != nil {
		return map[string]any{"clis": []any{}, "error": "the agent command isn't installed here, so provider sign-ins can't be checked"}
	}
	ctx, cancel := context.WithTimeout(context.Background(), accountsTimeout)
	defer cancel()
	command := exec.CommandContext(ctx, program, "accounts", "detect", "--json")
	command.Stdin = nil
	output, err := command.Output()
	report := map[string]any{}
	if err != nil || json.Unmarshal([]byte(lastLine(string(output))), &report) != nil {
		return map[string]any{"clis": []any{}, "error": "this computer's agent couldn't check provider sign-ins (an older agent? run agent update)"}
	}
	return report
}

func lastLine(text string) string {
	lines := strings.Split(strings.TrimSpace(text), "\n")
	return strings.TrimSpace(lines[len(lines)-1])
}
