package terminal

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"

	"ai-coding-agent-proxy/client/internal/settings"
)

const (
	// Pause between the pieces of one input (text, then Enter). TUIs like Claude Code
	// treat text and Enter arriving in one read as a paste rather than a submit.
	InputGap = 120 * time.Millisecond
	// Minimum spacing between separate inputs, so quick taps on the console's keys stay separate keypresses.
	KeyGap = 50 * time.Millisecond
	frame  = 256 * 1024
)

var retryDelays = []time.Duration{time.Second, 2 * time.Second, 5 * time.Second, 10 * time.Second}

// HostURL is the agent-proxy's WebSocket for sharing a session's terminal.
func HostURL(proxyURL, sessionID string) string {
	parsed, err := url.Parse(strings.TrimRight(proxyURL, "/"))
	if err != nil {
		return ""
	}
	scheme := "ws"
	if parsed.Scheme == "https" {
		scheme = "wss"
	}
	return fmt.Sprintf("%s://%s%s/api/terminals/%s/host", scheme, parsed.Host, parsed.Path, sessionID)
}

// ViewURL is where the console shows a session's terminal.
func ViewURL(serverURL, sessionID string) string {
	return strings.TrimRight(serverURL, "/") + "/terminals/" + sessionID
}

// Probe checks the server will accept this machine's terminals. It returns a problem to
// show, or "" when it will.
func Probe(proxyURL string) string {
	refused := "the agent-server doesn't accept terminals from this machine; sign it in with `agent login <server>`"
	response, err := settings.Client(5 * time.Second).Get(strings.TrimRight(proxyURL, "/") + "/api/remote")
	if err != nil {
		return fmt.Sprintf("couldn't reach the agent-server (%v); retrying in the background", err)
	}
	defer response.Body.Close()
	if response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden {
		return refused
	}
	if response.StatusCode >= 400 {
		return fmt.Sprintf("couldn't reach the agent-server (HTTP %d); retrying in the background", response.StatusCode)
	}
	var body struct {
		Trusted bool `json:"trusted"`
	}
	if json.NewDecoder(response.Body).Decode(&body) == nil && body.Trusted {
		return ""
	}
	return refused
}

// Share streams a History to the server and types the server's input into the agent.
type Share struct {
	URL     string
	Command string

	history  *History
	write    func([]byte)
	cols     int
	rows     int
	controls []map[string]any
	exit     map[string]any
	done     chan struct{}

	problemMu sync.Mutex
	problem   string
}

func NewShare(url, command string) *Share {
	return &Share{URL: url, Command: command, history: NewHistory(HistoryLimit), write: func([]byte) {}, cols: 80, rows: 24, done: make(chan struct{})}
}

// History is where the agent's output goes; Start may replace it.
func (s *Share) History() *History { return s.history }

// Problem is why the terminal isn't (or stopped being) shared, or "".
func (s *Share) Problem() string {
	s.problemMu.Lock()
	defer s.problemMu.Unlock()
	return s.problem
}

func (s *Share) setProblem(problem string) {
	s.problemMu.Lock()
	s.problem = problem
	s.problemMu.Unlock()
}

// Start connects in the background; write types into the agent.
func (s *Share) Start(history *History, write func([]byte), cols, rows int) {
	s.history, s.write, s.cols, s.rows = history, write, cols, rows
	go s.run()
}

func (s *Share) Resize(cols, rows int) {
	h := s.history
	h.mu.Lock()
	s.cols, s.rows = cols, rows
	s.controls = append(s.controls, map[string]any{"type": "resize", "cols": cols, "rows": rows})
	h.notifyLocked()
	h.mu.Unlock()
}

// Finish sends what's left and the exit status, waiting up to timeout for the server to get it.
func (s *Share) Finish(status int, timeout time.Duration) {
	h := s.history
	h.mu.Lock()
	s.exit = map[string]any{"type": "exit", "status": status}
	h.notifyLocked()
	h.mu.Unlock()
	select {
	case <-s.done:
	case <-time.After(timeout):
	}
}

type refusedError struct{ reason string }

func (e refusedError) Error() string { return e.reason }

func (s *Share) finished() bool {
	s.history.mu.Lock()
	defer s.history.mu.Unlock()
	return s.exit != nil
}

func (s *Share) run() {
	defer close(s.done)
	for attempt := 0; ; attempt++ {
		err := s.stream()
		if err == nil {
			return
		}
		var refused refusedError
		if errors.As(err, &refused) {
			s.setProblem(refused.reason)
			return
		}
		s.setProblem(fmt.Sprintf("lost the connection to the proxy (%v)", err))
		if s.finished() && attempt >= 1 {
			return
		}
		delay := retryDelays[min(attempt, len(retryDelays)-1)]
		unlock := s.history.Wait(func() bool { return s.exit != nil }, delay)
		unlock()
	}
}

func (s *Share) stream() error {
	dialer := websocket.Dialer{Proxy: nil, HandshakeTimeout: 5 * time.Second}
	conn, response, err := dialer.Dial(s.URL, nil)
	if err != nil {
		if response != nil && (response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden) {
			return refusedError{"the proxy needs you to sign in: run `agent login`"}
		}
		return err
	}
	defer conn.Close()
	s.history.mu.Lock()
	cols, rows := s.cols, s.rows
	s.history.mu.Unlock()
	hello, _ := json.Marshal(map[string]any{"type": "hello", "cols": cols, "rows": rows, "command": s.Command})
	// A refusing server may close before the hello is even read.
	_ = conn.WriteMessage(websocket.TextMessage, hello)
	_ = conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	_, message, err := conn.ReadMessage()
	if err != nil {
		var closed *websocket.CloseError
		if errors.As(err, &closed) && (closed.Code == 4401 || closed.Code == 4403 || closed.Code == 4404) {
			reason := closed.Text
			if reason == "" {
				reason = "the proxy refused the terminal"
			}
			return refusedError{reason}
		}
		return err
	}
	_ = conn.SetReadDeadline(time.Time{})
	var resume struct {
		From int64 `json:"from"`
	}
	_ = json.Unmarshal(message, &resume)
	cursor := resume.From
	s.setProblem("")
	lost := make(chan struct{})
	go s.receive(conn, lost)
	isLost := func() bool {
		select {
		case <-lost:
			return true
		default:
			return false
		}
	}
	h := s.history
	for {
		unlock := h.Wait(func() bool {
			return h.start+int64(len(h.data)) > cursor || len(s.controls) > 0 || s.exit != nil || isLost()
		}, 5*time.Second)
		if isLost() {
			unlock()
			return errors.New("the proxy closed the connection")
		}
		start, data := h.sinceLocked(cursor)
		controls := s.controls
		s.controls = nil
		finished := s.exit
		unlock()
		if start > cursor {
			if err := writeJSON(conn, map[string]any{"type": "gap", "from": start}); err != nil {
				return err
			}
		}
		for offset := 0; offset < len(data); offset += frame {
			if err := conn.WriteMessage(websocket.BinaryMessage, data[offset:min(offset+frame, len(data))]); err != nil {
				return err
			}
		}
		cursor = start + int64(len(data))
		for _, control := range controls {
			if err := writeJSON(conn, control); err != nil {
				return err
			}
		}
		if finished != nil && len(data) == 0 {
			if err := writeJSON(conn, finished); err != nil {
				return err
			}
			_ = conn.WriteControl(websocket.CloseMessage, websocket.FormatCloseMessage(websocket.CloseNormalClosure, ""), time.Now().Add(time.Second))
			return nil
		}
	}
}

// receive types the server's input into the agent, keeping the gaps between its pieces.
func (s *Share) receive(conn *websocket.Conn, lost chan struct{}) {
	defer func() {
		close(lost)
		s.history.Notify()
	}()
	var last time.Time
	for {
		kind, message, err := conn.ReadMessage()
		if err != nil {
			return
		}
		if kind != websocket.TextMessage {
			continue
		}
		var data struct {
			Type  string `json:"type"`
			Parts []any  `json:"parts"`
		}
		if json.Unmarshal(message, &data) != nil || data.Type != "input" {
			continue
		}
		for index, item := range data.Parts {
			part, ok := item.(string)
			if !ok {
				continue
			}
			gap := KeyGap
			if index > 0 {
				gap = InputGap
			}
			if wait := time.Until(last.Add(gap)); wait > 0 {
				time.Sleep(wait)
			}
			s.write([]byte(part))
			last = time.Now()
		}
	}
}

func writeJSON(conn *websocket.Conn, value any) error {
	data, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return conn.WriteMessage(websocket.TextMessage, data)
}
