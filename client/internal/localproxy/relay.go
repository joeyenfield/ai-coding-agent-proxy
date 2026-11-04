package localproxy

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"

	"ai-coding-agent-proxy/client/internal/settings"
)

// WebSocket close codes a peer may not send itself.
var reservedClose = map[int]bool{1005: true, 1006: true, 1015: true}

// The proxy checks Host and Origin itself (local), after accepting, so a refusal reaches
// the launcher as a close code it can show.
var upgrader = websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}

// How often an open socket's access token is checked, how long the current one goes unsent at most
// (a renewal the server missed is retried long before the old token expires), and how long the
// server's side may be silent before it's taken for dead (the server pings every 20 s).
var (
	renewEvery  = 10 * time.Second
	resendEvery = 60 * time.Second
	readTimeout = 70 * time.Second
)

// Dial opens a WebSocket to path on server, signed with the machine's sign-in. Like the
// HTTP client it never uses the environment's proxy.
func Dial(upstream settings.Upstream, server, path string) (*websocket.Conn, *http.Response, error) {
	conn, response, _, err := dial(upstream, server, path)
	return conn, response, err
}

// dial is Dial, also returning the access token the socket was opened with.
func dial(upstream settings.Upstream, server, path string) (*websocket.Conn, *http.Response, string, error) {
	parsed, err := url.Parse(server)
	if err != nil {
		return nil, nil, "", err
	}
	scheme := "ws"
	if parsed.Scheme == "https" {
		scheme = "wss"
	}
	target := fmt.Sprintf("%s://%s%s%s", scheme, parsed.Host, strings.TrimRight(parsed.Path, "/"), path)
	header := http.Header{}
	settings.Identify(header, true)
	token, err := upstream.AccessToken(server)
	if err != nil {
		return nil, nil, "", err
	}
	header.Set(settings.TokenHeader, token)
	dialer := websocket.Dialer{Proxy: nil, HandshakeTimeout: 10 * time.Second, EnableCompression: false}
	conn, response, err := dialer.Dial(target, header)
	return conn, response, token, err
}

// serverSide is the server's end of a relayed socket: one writer at a time (gorilla allows one),
// a write deadline, a read deadline the server's pings keep moving, and the machine's access token
// sent over it whenever it's refreshed. The server closes a socket (4410) once the token it opened
// with expires; renewing over it keeps a long reply, a kept-alive connection or a shared terminal up.
type serverSide struct {
	conn *websocket.Conn
	mu   sync.Mutex
	stop chan struct{}
	once sync.Once
	// The token the server holds for this socket: the dial's, then the last one sent over it. A 4410
	// means that one stopped working, so it's the one to reject.
	tokenMu sync.Mutex
	token   string
}

func newServerSide(upstream settings.Upstream, server string, conn *websocket.Conn, token string) *serverSide {
	side := &serverSide{conn: conn, stop: make(chan struct{}), token: token}
	alive := func() { _ = conn.SetReadDeadline(time.Now().Add(readTimeout)) }
	alive()
	conn.SetPingHandler(func(data string) error {
		alive()
		_ = conn.WriteControl(websocket.PongMessage, []byte(data), time.Now().Add(10*time.Second))
		return nil
	})
	conn.SetPongHandler(func(string) error { alive(); return nil })
	go side.renew(upstream, server, token)
	return side
}

func (s *serverSide) write(kind int, data []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	_ = s.conn.SetWriteDeadline(time.Now().Add(30 * time.Second))
	return s.conn.WriteMessage(kind, data)
}

// read is ReadMessage, keeping the read deadline moving while the server talks.
func (s *serverSide) read() (int, []byte, error) {
	kind, data, err := s.conn.ReadMessage()
	if err == nil {
		_ = s.conn.SetReadDeadline(time.Now().Add(readTimeout))
	}
	return kind, data, err
}

func (s *serverSide) close() { s.once.Do(func() { close(s.stop) }) }

// current is the token the server last accepted from this socket, as far as this side knows.
func (s *serverSide) current() string {
	s.tokenMu.Lock()
	defer s.tokenMu.Unlock()
	return s.token
}

func (s *serverSide) renew(upstream settings.Upstream, server, token string) {
	if token == "" {
		return
	}
	sent := time.Now()
	ticker := time.NewTicker(renewEvery)
	defer ticker.Stop()
	for {
		select {
		case <-s.stop:
			return
		case <-ticker.C:
			next, err := upstream.AccessToken(server)
			if err != nil || (next == token && time.Since(sent) < resendEvery) {
				continue
			}
			message, _ := json.Marshal(map[string]string{"type": "token", "token": next})
			if s.write(websocket.TextMessage, message) == nil {
				token, sent = next, time.Now()
				s.tokenMu.Lock()
				s.token = next
				s.tokenMu.Unlock()
			}
		}
	}
}

// terminal relays a terminal shared with --control to the server, signed as this machine.
func (p *Proxy) terminal(w http.ResponseWriter, r *http.Request, sessionID string) {
	launcher, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		return
	}
	defer launcher.Close()
	if !local(r) {
		closeWith(launcher, 4403, "agent-proxy only serves programs on this computer")
		return
	}
	server := p.Upstream.Server()
	remote, response, token, err := dial(p.Upstream, server, "/api/terminals/"+url.PathEscape(sessionID)+"/host")
	if err != nil {
		if errors.Is(err, settings.ErrLoginRequired) {
			closeWith(launcher, 4401, "OAuth sign-in required: run `agent login`")
		} else if response != nil && (response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden) {
			closeWith(launcher, 4400+response.StatusCode%100, "the agent-server refused this machine: run `agent login`")
		} else if response != nil {
			// Anything else (a reverse proxy's 502/503/504 while the server restarts) passes: the
			// launcher gives up on 4403 and 4404, so they mustn't stand for "try again later".
			closeWith(launcher, 1011, fmt.Sprintf("the agent-server at %s answered HTTP %d", server, response.StatusCode))
		} else {
			closeWith(launcher, 1011, fmt.Sprintf("couldn't reach the agent-server at %s: %v", server, err))
		}
		return
	}
	defer remote.Close()
	side := newServerSide(p.Upstream, server, remote, token)
	defer side.close()
	code, reason := relay(launcher, side)
	closeWith(remote, websocket.CloseNormalClosure, "")
	if code == 4410 {
		if err := p.Upstream.RejectAccessTokenIf(server, side.current()); err != nil {
			reason = err.Error()
		}
	}
	// A token expiring on an established socket is retryable. The next dial refreshes;
	// a revoked credential then fails the handshake instead.
	if code == 4401 {
		code = 1011
	}
	// Pass the server's close on, so the launcher sees a refusal (4401, 4404) for what it is.
	if code != 0 {
		if reservedClose[code] {
			code = 1011
		}
		closeWith(launcher, code, reason)
	}
}

// relay copies messages both ways until either side ends. It returns how the server
// (second) side closed, when it did.
func relay(local *websocket.Conn, server *serverSide) (int, string) {
	var once sync.Once
	done := make(chan struct{})
	finish := func() { once.Do(func() { close(done) }) }
	var mu sync.Mutex
	var code int
	var reason string
	go func() {
		defer finish()
		for {
			kind, data, err := local.ReadMessage()
			if err != nil {
				return
			}
			if server.write(kind, data) != nil {
				return
			}
		}
	}()
	go func() {
		defer finish()
		for {
			kind, data, err := server.read()
			if err != nil {
				var closed *websocket.CloseError
				if errors.As(err, &closed) {
					mu.Lock()
					code, reason = closed.Code, closed.Text
					mu.Unlock()
				}
				return
			}
			if local.WriteMessage(kind, data) != nil {
				return
			}
		}
	}()
	<-done
	// Give the other direction a moment to record a close that's already on its way.
	time.Sleep(50 * time.Millisecond)
	mu.Lock()
	defer mu.Unlock()
	return code, reason
}

func closeWith(conn *websocket.Conn, code int, reason string) {
	if len(reason) > 120 {
		reason = reason[:120]
	}
	_ = conn.WriteControl(websocket.CloseMessage, websocket.FormatCloseMessage(code, reason), time.Now().Add(time.Second))
}

// ServeTunnels accepts account agents' connections to the HTTPS proxy port, each carried
// to the server's intercept over its own WebSocket.
func (p *Proxy) ServeTunnels(listener net.Listener) error {
	delay := 5 * time.Millisecond
	for {
		conn, err := listener.Accept()
		if err != nil {
			// Out of file handles and the like pass; only a closed listener ends it (as http.Server does).
			if errors.Is(err, net.ErrClosed) {
				return err
			}
			if p.Logger != nil {
				p.Logger.Printf("HTTPS proxy: accept: %v; retrying in %v", err, delay)
			}
			time.Sleep(delay)
			delay = min(delay*2, time.Second)
			continue
		}
		delay = 5 * time.Millisecond
		go p.tunnel(conn)
	}
}

func (p *Proxy) tunnel(conn net.Conn) {
	defer conn.Close()
	server := p.Upstream.Server()
	remote, response, token, err := dial(p.Upstream, server, "/api/proxy/tunnel")
	if err != nil {
		if response != nil {
			status := 502
			if response.StatusCode == 401 || response.StatusCode == 403 {
				status = 407
			}
			reason := fmt.Sprintf("the agent-server answered %d: run `agent login %s`", response.StatusCode, server)
			p.refuseTunnel(readHead(conn), status, reason)
		} else if errors.Is(err, settings.ErrLoginRequired) {
			p.refuseTunnel(readHead(conn), 407, "OAuth sign-in required: run `agent login`")
		} else {
			p.refuseTunnel(readHead(conn), 502, fmt.Sprintf("couldn't reach the agent-server at %s: %v", server, err))
		}
		return
	}
	defer remote.Close()
	side := newServerSide(p.Upstream, server, remote, token)
	defer side.close()
	var once sync.Once
	done := make(chan struct{})
	finish := func() { once.Do(func() { close(done) }) }
	var answered bool
	var closeCode int
	var closeReason string
	var mu sync.Mutex
	go func() {
		defer finish()
		buffer := make([]byte, 65536)
		for {
			n, err := conn.Read(buffer)
			if n > 0 && side.write(websocket.BinaryMessage, buffer[:n]) != nil {
				return
			}
			if err != nil {
				return
			}
		}
	}()
	go func() {
		defer finish()
		for {
			kind, data, err := side.read()
			if err != nil {
				var closed *websocket.CloseError
				if errors.As(err, &closed) {
					mu.Lock()
					closeCode, closeReason = closed.Code, closed.Text
					mu.Unlock()
				}
				return
			}
			if kind != websocket.BinaryMessage {
				continue
			}
			mu.Lock()
			answered = true
			mu.Unlock()
			if _, err := conn.Write(data); err != nil {
				return
			}
		}
	}()
	<-done
	time.Sleep(50 * time.Millisecond)
	mu.Lock()
	defer mu.Unlock()
	if closeCode == 4410 {
		if err := p.Upstream.RejectAccessTokenIf(server, side.current()); err != nil {
			closeReason = err.Error()
		}
	}
	if !answered && closeCode >= 4000 {
		status := 502
		if closeCode == 4401 || closeCode == 4403 {
			status = 407
		}
		if closeReason == "" {
			closeReason = "the agent-server refused"
		}
		p.refuseTunnel(conn, status, closeReason)
	}
}

func (p *Proxy) refuseTunnel(conn net.Conn, status int, reason string) {
	if p.Logger != nil {
		p.Logger.Printf("HTTPS proxy connection refused (%d): %s", status, reason)
	}
	phrase := "Bad Gateway"
	if status == 407 {
		phrase = "Proxy Authentication Required"
	}
	reason = strings.Join(strings.Fields(reason), " ")
	_, _ = io.WriteString(conn, fmt.Sprintf("HTTP/1.1 %d %s\r\nX-Agent-Proxy-Error: %s\r\nContent-Length: 0\r\nConnection: close\r\n\r\n", status, phrase, reason))
	// Close our side only, so the answer arrives before the connection ends.
	if tcp, ok := conn.(*net.TCPConn); ok {
		_ = tcp.CloseWrite()
		_ = conn.SetReadDeadline(time.Now().Add(time.Second))
		_, _ = io.Copy(io.Discard, conn)
	}
}

// readHead reads the agent's request head (its CONNECT) before a refusal: closing a
// socket with unread data resets it on Windows, and the agent would never see the answer.
func readHead(conn net.Conn) net.Conn {
	_ = conn.SetReadDeadline(time.Now().Add(500 * time.Millisecond))
	var head []byte
	buffer := make([]byte, 4096)
	for !strings.Contains(string(head), "\r\n\r\n") && len(head) < 256*1024 {
		n, err := conn.Read(buffer)
		head = append(head, buffer[:n]...)
		if err != nil {
			break
		}
	}
	_ = conn.SetReadDeadline(time.Time{})
	return conn
}
