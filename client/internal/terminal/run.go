package terminal

import (
	"os"
	"strings"
	"time"
	"unicode/utf8"

	"golang.org/x/term"
)

// child is the agent running in a pseudo-terminal.
type child interface {
	Read([]byte) (int, error)
	Write([]byte)
	Resize(cols, rows int)
	// Wait returns the exit status once the agent exits.
	Wait() int
	// Close ends the pseudo-terminal, so Read reports the end once the output is drained.
	Close()
}

// console is the user's own terminal, in raw mode while the agent runs.
type console interface {
	Raw() error
	Restore()
	// Watch calls resized whenever the terminal changes size, until stop is closed.
	Watch(resized func(), stop <-chan struct{})
}

// withColor names the terminal type for an agent in a pseudo-terminal when the environment doesn't:
// a container started without -t, or a service, has no TERM, and agents (Claude Code, Codex) then
// print without color. The console's terminal and every terminal people use understand xterm's
// 256 colors and 24-bit color. A TERM someone set is kept, except dumb, which a pty isn't.
func withColor(env []string) []string {
	out := make([]string, 0, len(env)+2)
	hasColorTerm := false
	for _, entry := range env {
		switch {
		case entry == "TERM=" || entry == "TERM=dumb":
			continue
		case strings.HasPrefix(entry, "COLORTERM="):
			hasColorTerm = true
		}
		out = append(out, entry)
	}
	hasTerm := false
	for _, entry := range out {
		if strings.HasPrefix(entry, "TERM=") {
			hasTerm = true
		}
	}
	if !hasTerm {
		out = append(out, "TERM=xterm-256color")
		if !hasColorTerm {
			out = append(out, "COLORTERM=truecolor")
		}
	}
	return out
}

// Unavailable is why the agent can't run in a shared terminal here, or "".
func Unavailable() string {
	if !term.IsTerminal(int(os.Stdin.Fd())) || !term.IsTerminal(int(os.Stdout.Fd())) {
		return "this isn't an interactive terminal"
	}
	return platformUnavailable()
}

// Size of this terminal, with a fallback for when it can't be read.
func Size() (int, int) {
	cols, rows, err := term.GetSize(int(os.Stdout.Fd()))
	if err != nil || cols < 2 || rows < 2 {
		return 120, 40
	}
	return cols, rows
}

// Run runs command in a pseudo-terminal mirrored to this one, shared through share when
// it isn't nil. It returns the agent's exit status.
func Run(command []string, dir string, env []string, share *Share) (int, error) {
	cols, rows := Size()
	agent, err := startChild(command, dir, env, cols, rows)
	if err != nil {
		return 127, err
	}
	history := NewHistory(HistoryLimit)
	if share != nil {
		history = share.History()
		share.Start(history, agent.Write, cols, rows)
	}
	screen := newConsole()
	if err := screen.Raw(); err != nil {
		agent.Close()
		return 1, err
	}
	output := make(chan struct{})
	go func() {
		defer close(output)
		buffer := make([]byte, 65536)
		var pending []byte
		for {
			n, err := agent.Read(buffer)
			if n > 0 {
				history.Append(buffer[:n])
				// Whole characters only: the console would draw a split one as two bad ones.
				pending = append(pending, buffer[:n]...)
				complete := completeUTF8(pending)
				_, _ = os.Stdout.Write(pending[:complete])
				pending = append([]byte(nil), pending[complete:]...)
			}
			if err != nil {
				if len(pending) > 0 {
					_, _ = os.Stdout.Write(pending)
				}
				return
			}
		}
	}()
	go func() {
		buffer := make([]byte, 4096)
		for {
			n, err := os.Stdin.Read(buffer)
			if n > 0 {
				agent.Write(append([]byte(nil), buffer[:n]...))
			}
			if err != nil {
				return
			}
		}
	}()
	stop := make(chan struct{})
	size := [2]int{cols, rows}
	go screen.Watch(func() {
		cols, rows := Size()
		if [2]int{cols, rows} != size {
			size = [2]int{cols, rows}
			agent.Resize(cols, rows)
			if share != nil {
				share.Resize(cols, rows)
			}
		}
	}, stop)
	status := agent.Wait()
	close(stop)
	agent.Close()
	// Let the last of the agent's output reach the screen (and the server).
	select {
	case <-output:
	case <-time.After(2 * time.Second):
	}
	screen.Restore()
	if share != nil {
		// Long enough for a reconnect or two (the delays are 1, 2, 5 s), so the exit isn't lost to a blip.
		share.Finish(status, 10*time.Second)
	}
	return status, nil
}

// HeadlessSize is the terminal an agent started from the server gets; nobody's window sets it.
const HeadlessCols, HeadlessRows = 120, 36

// RunHeadless runs command in a pseudo-terminal with no local window: what it prints goes
// only to the server through share, and only the server's viewers type into it. This is
// how an agent started from the console runs on a fleet machine.
func RunHeadless(command []string, dir string, env []string, share *Share) (int, error) {
	agent, err := startChild(command, dir, env, HeadlessCols, HeadlessRows)
	if err != nil {
		return 127, err
	}
	history := share.History()
	share.Start(history, agent.Write, HeadlessCols, HeadlessRows)
	output := make(chan struct{})
	go func() {
		defer close(output)
		buffer := make([]byte, 65536)
		for {
			n, err := agent.Read(buffer)
			if n > 0 {
				history.Append(buffer[:n])
			}
			if err != nil {
				return
			}
		}
	}()
	status := agent.Wait()
	agent.Close()
	select {
	case <-output:
	case <-time.After(2 * time.Second):
	}
	share.Finish(status, 15*time.Second)
	return status, nil
}

// completeUTF8 is the length of data's prefix that doesn't end inside a character.
func completeUTF8(data []byte) int {
	for back := 1; back <= utf8.UTFMax && back <= len(data); back++ {
		index := len(data) - back
		if utf8.RuneStart(data[index]) {
			if !utf8.FullRune(data[index:]) {
				return index
			}
			return len(data)
		}
	}
	return len(data)
}
