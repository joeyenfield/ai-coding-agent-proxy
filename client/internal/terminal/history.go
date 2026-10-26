// Package terminal runs an agent in a pseudo-terminal: mirrored to this terminal and
// shared with the server.
//
// Like a one-window tmux. The agent draws in your terminal as usual, while the launcher
// keeps a copy of everything it prints and streams it to the server (through this
// machine's agent-proxy), which shows it, scrollback included, in the console on any
// signed-in or paired device, and can type into it. On Windows the agent runs in a
// ConPTY; elsewhere in a POSIX pty.
//
// The local session never depends on the server: if the connection drops, the launcher
// keeps retrying in the background and resends what the server missed.
package terminal

import (
	"sync"
	"time"
)

// HistoryLimit is how much of what the agent printed is kept for resending.
const HistoryLimit = 4 * 1024 * 1024

// History is what the agent printed, as one byte stream with absolute offsets, keeping
// the newest bytes. Changes wake anyone waiting in Wait.
type History struct {
	mu      sync.Mutex
	limit   int
	data    []byte
	start   int64
	changed chan struct{}
}

func NewHistory(limit int) *History {
	return &History{limit: limit, changed: make(chan struct{})}
}

func (h *History) Append(chunk []byte) {
	h.mu.Lock()
	h.data = append(h.data, chunk...)
	if excess := len(h.data) - h.limit; excess > 0 {
		h.data = append([]byte(nil), h.data[excess:]...)
		h.start += int64(excess)
	}
	h.notifyLocked()
	h.mu.Unlock()
}

// End is the offset just past the newest byte.
func (h *History) End() int64 {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.start + int64(len(h.data))
}

// Since is the bytes from offset on, or from the oldest kept byte if offset was dropped.
func (h *History) Since(offset int64) (int64, []byte) {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.sinceLocked(offset)
}

func (h *History) sinceLocked(offset int64) (int64, []byte) {
	if offset < h.start {
		offset = h.start
	}
	return offset, append([]byte(nil), h.data[offset-h.start:]...)
}

// Notify wakes every waiter, for changes other than new output.
func (h *History) Notify() {
	h.mu.Lock()
	h.notifyLocked()
	h.mu.Unlock()
}

func (h *History) notifyLocked() {
	close(h.changed)
	h.changed = make(chan struct{})
}

// Wait blocks until ready (checked with the lock held) is true, or the timeout passes.
// It returns with the lock held; call unlock when done.
func (h *History) Wait(ready func() bool, timeout time.Duration) (unlock func()) {
	deadline := time.Now().Add(timeout)
	h.mu.Lock()
	for !ready() {
		remaining := time.Until(deadline)
		if remaining <= 0 {
			break
		}
		changed := h.changed
		h.mu.Unlock()
		timer := time.NewTimer(remaining)
		select {
		case <-changed:
		case <-timer.C:
		}
		timer.Stop()
		h.mu.Lock()
	}
	return h.mu.Unlock
}
