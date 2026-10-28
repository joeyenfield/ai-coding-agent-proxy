package mcp

import (
	"bufio"
	"bytes"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"

	"ai-coding-agent-proxy/machine/internal/process"
	"ai-coding-agent-proxy/machine/internal/settings"
)

// KeyHeader carries the bridge's key on every request from the server; it never reaches an MCP server.
const KeyHeader = "X-AI-Proxy-MCP-Key"

const (
	sessionHeader = "Mcp-Session-Id"
	// A stdio session nobody has used for this long is ended (agents rarely say goodbye).
	idleTimeout = 6 * time.Hour
	// At most this many stdio processes at once, over all servers.
	maxSessions = 32
	maxBody     = 16 << 20
	pingEvery   = 30 * time.Second
	// Messages kept for a session's GET stream while none is open.
	backlogSize = 100
)

// Bridge is the loopback HTTP server the fleet's tunnel reaches this machine's MCP servers on.
type Bridge struct {
	Logger *log.Logger
	// Load lists the servers; tests replace it. By default it reads mcp.json, so `agent-machine
	// mcp add` applies to the next MCP session without a restart.
	Load func() ([]Server, error)

	mu       sync.Mutex
	listener net.Listener
	key      string
	sessions map[string]*session
	proxy    *httputil.ReverseProxy
}

// Describe is the bridge for the hello: its port and key, and the servers by name (never their
// commands, environments or URLs). It starts the bridge the first time there are servers to share.
func (b *Bridge) Describe() map[string]any {
	servers, err := b.load()
	if err != nil {
		b.logf("mcp: %v", err)
	}
	if len(servers) == 0 {
		return nil
	}
	port, key, err := b.Start()
	if err != nil {
		b.logf("mcp: %v", err)
		return nil
	}
	listed := []map[string]any{}
	for _, server := range servers {
		listed = append(listed, map[string]any{
			"name": server.Name, "description": server.Description, "transport": server.Transport(), "found": server.Found(),
		})
	}
	return map[string]any{"port": port, "key": key, "servers": listed}
}

// Port is the bridge's port, 0 before it starts.
func (b *Bridge) Port() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.listener == nil {
		return 0
	}
	return b.listener.Addr().(*net.TCPAddr).Port
}

// Start listens on a loopback port, once, with a new key.
func (b *Bridge) Start() (int, string, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.listener == nil {
		listener, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			return 0, "", err
		}
		secret := make([]byte, 24)
		_, _ = rand.Read(secret)
		b.listener, b.key, b.sessions = listener, hex.EncodeToString(secret), map[string]*session{}
		b.proxy = &httputil.ReverseProxy{Rewrite: func(*httputil.ProxyRequest) {}, Transport: settings.Transport(), FlushInterval: -1}
		go func() { _ = (&http.Server{Handler: b, ReadHeaderTimeout: 30 * time.Second}).Serve(listener) }()
		go b.reap()
	}
	return b.listener.Addr().(*net.TCPAddr).Port, b.key, nil
}

// Close ends every stdio session and stops listening.
func (b *Bridge) Close() {
	b.mu.Lock()
	listener, sessions := b.listener, b.sessions
	b.listener, b.sessions = nil, nil
	b.mu.Unlock()
	if listener != nil {
		_ = listener.Close()
	}
	for _, s := range sessions {
		s.close()
	}
}

func (b *Bridge) load() ([]Server, error) {
	if b.Load != nil {
		return b.Load()
	}
	return Load()
}

func (b *Bridge) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	b.mu.Lock()
	key := b.key
	b.mu.Unlock()
	if key == "" || subtle.ConstantTimeCompare([]byte(r.Header.Get(KeyHeader)), []byte(key)) != 1 {
		writeError(w, http.StatusUnauthorized, "not the server's key")
		return
	}
	r.Header.Del(KeyHeader)
	serverName, ok := strings.CutPrefix(r.URL.Path, "/mcp/")
	if !ok {
		writeError(w, http.StatusNotFound, "not found")
		return
	}
	servers, err := b.load()
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	for _, server := range servers {
		if server.Name == serverName {
			if server.URL != "" {
				b.forward(w, r, server)
			} else {
				b.stdio(w, r, server)
			}
			return
		}
	}
	writeError(w, http.StatusNotFound, "this machine shares no MCP server called "+serverName)
}

// forward passes a request to a URL server as it is, with its own headers added.
func (b *Bridge) forward(w http.ResponseWriter, r *http.Request, server Server) {
	target, _ := url.Parse(server.URL)
	if r.URL.RawQuery != "" {
		target.RawQuery = strings.TrimPrefix(target.RawQuery+"&"+r.URL.RawQuery, "&")
	}
	out := r.Clone(r.Context())
	out.URL, out.Host, out.RequestURI = target, target.Host, ""
	for header, value := range server.Headers {
		out.Header.Set(header, value)
	}
	b.proxy.ServeHTTP(w, out)
}

// stdio is Streamable HTTP for a stdio server: initialize starts its process, POSTs write to it,
// and what it says comes back on the POST's event stream (or the session's GET stream).
func (b *Bridge) stdio(w http.ResponseWriter, r *http.Request, server Server) {
	id := r.Header.Get(sessionHeader)
	var s *session
	if id != "" {
		b.mu.Lock()
		s = b.sessions[id]
		b.mu.Unlock()
		if s == nil || s.server != server.Name {
			writeError(w, http.StatusNotFound, "no such MCP session: initialize again")
			return
		}
		s.touch()
	}
	switch r.Method {
	case http.MethodPost:
		body, err := io.ReadAll(io.LimitReader(r.Body, maxBody))
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		messages, err := split(body)
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		if s == nil {
			if !initializes(messages) {
				writeError(w, http.StatusBadRequest, "start with initialize: there's no "+sessionHeader)
				return
			}
			if s, err = b.open(server); err != nil {
				b.logf("mcp %s: %v", server.Name, err)
				writeError(w, http.StatusBadGateway, err.Error())
				return
			}
		}
		s.post(w, r, messages)
	case http.MethodGet:
		if s == nil {
			writeError(w, http.StatusBadRequest, "name the session in "+sessionHeader)
			return
		}
		if !strings.Contains(r.Header.Get("Accept"), "text/event-stream") {
			writeError(w, http.StatusNotAcceptable, "accept text/event-stream")
			return
		}
		s.listen(w, r)
	case http.MethodDelete:
		if s == nil {
			writeError(w, http.StatusBadRequest, "name the session in "+sessionHeader)
			return
		}
		b.drop(s)
		s.close()
		w.WriteHeader(http.StatusOK)
	default:
		writeError(w, http.StatusMethodNotAllowed, "POST, GET or DELETE")
	}
}

// open starts a stdio server's process for a new MCP session.
func (b *Bridge) open(server Server) (*session, error) {
	b.mu.Lock()
	full := len(b.sessions) >= maxSessions
	b.mu.Unlock()
	if full {
		return nil, fmt.Errorf("this machine already runs %d MCP sessions; end some agents first", maxSessions)
	}
	program, err := process.LookPath(server.Command[0])
	if err != nil {
		return nil, fmt.Errorf("%s isn't installed here (%v)", server.Command[0], err)
	}
	cmd := exec.Command(program, server.Command[1:]...)
	cmd.Dir = server.Dir
	cmd.Env = os.Environ()
	for name, value := range server.Env {
		cmd.Env = append(cmd.Env, name+"="+value)
	}
	process.Group(cmd)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return nil, err
	}
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("couldn't start %s: %v", server.Command[0], err)
	}
	secret := make([]byte, 16)
	_, _ = rand.Read(secret)
	s := &session{
		id: hex.EncodeToString(secret), server: server.Name, cmd: cmd, stdin: stdin, logf: b.logf,
		waiting: map[string]*stream{}, done: make(chan struct{}), used: time.Now(),
	}
	b.mu.Lock()
	b.sessions[s.id] = s
	b.mu.Unlock()
	go s.read(stdout)
	go func() {
		scanner := bufio.NewScanner(stderr)
		scanner.Buffer(make([]byte, 64*1024), 1<<20)
		for scanner.Scan() {
			b.logf("mcp %s: %s", server.Name, scanner.Text())
		}
	}()
	go func() {
		err := cmd.Wait()
		b.logf("mcp %s: session %s ended (%v)", server.Name, s.id[:8], err)
		b.drop(s)
		s.ended()
	}()
	b.logf("mcp %s: session %s started", server.Name, s.id[:8])
	return s, nil
}

func (b *Bridge) drop(s *session) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.sessions[s.id] == s {
		delete(b.sessions, s.id)
	}
}

// reap ends sessions left idle: an agent that exits seldom ends its MCP sessions.
func (b *Bridge) reap() {
	for range time.Tick(time.Minute) {
		b.mu.Lock()
		if b.sessions == nil {
			b.mu.Unlock()
			return
		}
		var idle []*session
		for _, s := range b.sessions {
			if s.idle(idleTimeout) {
				idle = append(idle, s)
			}
		}
		b.mu.Unlock()
		for _, s := range idle {
			b.drop(s)
			s.close()
		}
	}
}

func (b *Bridge) logf(format string, args ...any) {
	if b.Logger != nil {
		b.Logger.Printf(format, args...)
	}
}

// session is one MCP session with a stdio server: its process, and the streams waiting on it.
type session struct {
	id     string
	server string
	cmd    *exec.Cmd
	stdin  io.WriteCloser
	logf   func(string, ...any)

	writeMu sync.Mutex
	mu      sync.Mutex
	// The open POST stream waiting on each request id, and every open POST stream, newest last.
	waiting map[string]*stream
	posts   []*stream
	// The session's GET stream, and what came while none was open.
	listening *stream
	backlog   [][]byte
	used      time.Time
	done      chan struct{}
	once      sync.Once
}

// stream is one response the session's messages go out on.
type stream struct {
	messages chan []byte
	closed   chan struct{}
	// Whether it's an event stream, which takes any message; a JSON reply takes only responses.
	events bool
}

func newStream(events bool) *stream {
	return &stream{messages: make(chan []byte, 64), closed: make(chan struct{}), events: events}
}

func (s *session) touch() {
	s.mu.Lock()
	s.used = time.Now()
	s.mu.Unlock()
}

func (s *session) idle(limit time.Duration) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.posts) == 0 && s.listening == nil && time.Since(s.used) > limit
}

func (s *session) write(messages []json.RawMessage) error {
	s.writeMu.Lock()
	defer s.writeMu.Unlock()
	for _, message := range messages {
		var compact bytes.Buffer
		if err := json.Compact(&compact, message); err != nil {
			return err
		}
		compact.WriteByte('\n')
		if _, err := s.stdin.Write(compact.Bytes()); err != nil {
			return err
		}
	}
	return nil
}

// post writes the client's messages, and answers with what the server says back: 202 for only
// notifications and responses, else an event stream (or JSON, if that's all the client takes)
// that ends with the last response.
func (s *session) post(w http.ResponseWriter, r *http.Request, messages []json.RawMessage) {
	ids := []string{}
	for _, message := range messages {
		if id, ok := requestID(message); ok {
			ids = append(ids, id)
		}
	}
	if len(ids) == 0 {
		if err := s.write(messages); err != nil {
			writeError(w, http.StatusBadGateway, "the MCP server has stopped")
			return
		}
		w.WriteHeader(http.StatusAccepted)
		return
	}
	events := strings.Contains(r.Header.Get("Accept"), "text/event-stream")
	out := newStream(events)
	s.mu.Lock()
	for _, id := range ids {
		s.waiting[id] = out
	}
	s.posts = append(s.posts, out)
	s.mu.Unlock()
	defer s.forget(out, ids)
	if err := s.write(messages); err != nil {
		writeError(w, http.StatusBadGateway, "the MCP server has stopped")
		return
	}
	w.Header().Set(sessionHeader, s.id)
	flusher, _ := w.(http.Flusher)
	if events {
		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Cache-Control", "no-cache")
		w.WriteHeader(http.StatusOK)
		if flusher != nil {
			flusher.Flush()
		}
	}
	remaining := len(ids)
	var replies []json.RawMessage
	for remaining > 0 {
		select {
		case message := <-out.messages:
			if _, ok := responseID(message); ok {
				remaining--
			}
			if !events {
				replies = append(replies, message)
				continue
			}
			fmt.Fprintf(w, "event: message\ndata: %s\n\n", message)
			if flusher != nil {
				flusher.Flush()
			}
		case <-r.Context().Done():
			return
		case <-s.done:
			if !events {
				writeError(w, http.StatusBadGateway, "the MCP server stopped before answering")
			}
			return
		}
	}
	if !events {
		w.Header().Set("Content-Type", "application/json")
		if len(messages) == 1 && len(replies) == 1 {
			_, _ = w.Write(replies[0])
			return
		}
		data, _ := json.Marshal(replies)
		_, _ = w.Write(data)
	}
}

func (s *session) forget(out *stream, ids []string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	close(out.closed)
	for _, id := range ids {
		if s.waiting[id] == out {
			delete(s.waiting, id)
		}
	}
	for index, item := range s.posts {
		if item == out {
			s.posts = append(s.posts[:index], s.posts[index+1:]...)
			break
		}
	}
	s.used = time.Now()
}

// listen is the session's GET stream: what the server says outside any request's reply.
func (s *session) listen(w http.ResponseWriter, r *http.Request) {
	out := newStream(true)
	s.mu.Lock()
	if s.listening != nil {
		// A client opens one; a new one replaces the old (a reconnect after a dropped connection).
		close(s.listening.closed)
	}
	s.listening = out
	backlog := s.backlog
	s.backlog = nil
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		if s.listening == out {
			s.listening = nil
			select {
			case <-out.closed:
			default:
				close(out.closed)
			}
		}
		s.used = time.Now()
		s.mu.Unlock()
	}()
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.WriteHeader(http.StatusOK)
	flusher, _ := w.(http.Flusher)
	send := func(message []byte) {
		fmt.Fprintf(w, "event: message\ndata: %s\n\n", message)
	}
	for _, message := range backlog {
		send(message)
	}
	if flusher != nil {
		flusher.Flush()
	}
	ping := time.NewTicker(pingEvery)
	defer ping.Stop()
	for {
		select {
		case message := <-out.messages:
			send(message)
		case <-ping.C:
			// A comment: clients ignore it, and proxies on the way don't time the stream out.
			_, _ = io.WriteString(w, ": ping\n\n")
		case <-out.closed:
			return
		case <-r.Context().Done():
			return
		case <-s.done:
			return
		}
		if flusher != nil {
			flusher.Flush()
		}
	}
}

// read takes the server's messages, one JSON value per line, to the stream each belongs on.
func (s *session) read(stdout io.Reader) {
	reader := bufio.NewReaderSize(stdout, 64*1024)
	for {
		line, err := reader.ReadBytes('\n')
		if trimmed := bytes.TrimSpace(line); len(trimmed) > 0 {
			if json.Valid(trimmed) {
				s.route(append([]byte(nil), trimmed...))
			} else {
				s.logf("mcp %s: not JSON on stdout: %.200s", s.server, trimmed)
			}
		}
		if err != nil {
			return
		}
	}
}

// route sends a response to the request's stream; anything else (notifications, the server's own
// requests) to the newest open event stream, the GET stream, or the backlog for the next GET.
func (s *session) route(message []byte) {
	s.mu.Lock()
	var out *stream
	if id, ok := responseID(message); ok {
		out = s.waiting[id]
	}
	if out == nil {
		for index := len(s.posts) - 1; index >= 0 && out == nil; index-- {
			if s.posts[index].events {
				out = s.posts[index]
			}
		}
	}
	if out == nil {
		out = s.listening
	}
	if out == nil {
		if len(s.backlog) < backlogSize {
			s.backlog = append(s.backlog, message)
		}
		s.mu.Unlock()
		return
	}
	s.mu.Unlock()
	select {
	case out.messages <- message:
	case <-out.closed:
	case <-s.done:
	}
}

// close ends the process: its stdin first, so it can stop by itself, then the process and what it started.
func (s *session) close() {
	_ = s.stdin.Close()
	select {
	case <-s.done:
		return
	case <-time.After(2 * time.Second):
	}
	if s.cmd.Process != nil {
		if process.StopTree(s.cmd.Process.Pid) != nil {
			_ = s.cmd.Process.Kill()
		}
	}
}

func (s *session) ended() {
	s.once.Do(func() { close(s.done) })
}

// split is a POST's body as its messages: one JSON-RPC message, or a batch of them.
func split(body []byte) ([]json.RawMessage, error) {
	body = bytes.TrimSpace(body)
	if len(body) > 0 && body[0] == '[' {
		var batch []json.RawMessage
		if err := json.Unmarshal(body, &batch); err != nil || len(batch) == 0 {
			return nil, errors.New("send a JSON-RPC message or a batch of them")
		}
		return batch, nil
	}
	var probe map[string]json.RawMessage
	if err := json.Unmarshal(body, &probe); err != nil {
		return nil, errors.New("send a JSON-RPC message or a batch of them")
	}
	return []json.RawMessage{body}, nil
}

type envelope struct {
	ID     json.RawMessage `json:"id"`
	Method string          `json:"method"`
	Result json.RawMessage `json:"result"`
	Error  json.RawMessage `json:"error"`
}

func initializes(messages []json.RawMessage) bool {
	for _, message := range messages {
		var parsed envelope
		if json.Unmarshal(message, &parsed) == nil && parsed.Method == "initialize" {
			return true
		}
	}
	return false
}

// requestID is a request's id, as a key: requests have a method and an id.
func requestID(message []byte) (string, bool) {
	var parsed envelope
	if json.Unmarshal(message, &parsed) != nil || parsed.Method == "" || len(parsed.ID) == 0 || string(parsed.ID) == "null" {
		return "", false
	}
	return idKey(parsed.ID), true
}

// responseID is a response's id, as a key: responses have an id and a result or an error.
func responseID(message []byte) (string, bool) {
	var parsed envelope
	if json.Unmarshal(message, &parsed) != nil || parsed.Method != "" || len(parsed.ID) == 0 || (parsed.Result == nil && parsed.Error == nil) {
		return "", false
	}
	return idKey(parsed.ID), true
}

// idKey writes an id the same way whichever way it came (1 and 1.0, spacing).
func idKey(raw json.RawMessage) string {
	var value any
	if json.Unmarshal(raw, &value) != nil {
		return string(raw)
	}
	data, _ := json.Marshal(value)
	return string(data)
}

func writeError(w http.ResponseWriter, status int, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	data, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": nil, "error": map[string]any{"code": -32000, "message": message}})
	_, _ = w.Write(data)
}
