package presence

import (
	"encoding/json"
	"fmt"
	"io"
	"net"
	"strconv"
	"sync"
	"time"

	"github.com/gorilla/websocket"

	"ai-control-mesh/machine/internal/providers"
)

// Tunnel is the server's {"type": "tunnel"} message: carry one connection from the server to
// a model server here, for agents' requests the server can't send over the network. The
// machine dials a WebSocket back for it (/api/machines/tunnel/<id>) and passes its bytes
// to and from 127.0.0.1:<port>, so a machine behind NAT, in WSL or on another network
// serves models without any port forward: the fleet connection is a mesh.
type Tunnel struct {
	Type string `json:"type"`
	ID   string `json:"tunnel"`
	Port int    `json:"port"`
}

// tunnelPorts remembers which ports tunnels may reach: the model servers this machine
// reported and the containers it runs for the server. Nothing else here is ever reached.
type tunnelPorts struct {
	mu    sync.Mutex
	ports map[int]bool
}

func (t *tunnelPorts) set(ports []int) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.ports = map[int]bool{}
	for _, port := range ports {
		t.ports[port] = true
	}
}

func (t *tunnelPorts) allowed(port int) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.ports[port]
}

// tunnel carries one connection: the server's WebSocket on one side, the model server on the other.
func (p *Presence) tunnel(server string, request Tunnel, extra []int) error {
	allowed := p.tunnels.allowed(request.Port)
	for _, port := range extra {
		allowed = allowed || port == request.Port
	}
	if !allowed || request.ID == "" {
		return fmt.Errorf("port %d isn't one of this machine's model servers", request.Port)
	}
	conn, _, err := p.Dial(server, "/api/machines/tunnel/"+request.ID)
	if err != nil {
		return err
	}
	defer conn.Close()
	local, err := net.DialTimeout("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(request.Port)), 10*time.Second)
	if err != nil {
		_ = conn.WriteControl(websocket.CloseMessage, websocket.FormatCloseMessage(4502, "the model server isn't answering"), time.Now().Add(time.Second))
		return err
	}
	defer local.Close()
	providers.Activity.TunnelOpened()
	defer providers.Activity.TunnelClosed()
	done := make(chan struct{}, 2)
	// One writer at a time on the WebSocket, as gorilla needs: the model server's bytes and the tokens.
	var writeMu sync.Mutex
	write := func(kind int, data []byte) error {
		writeMu.Lock()
		defer writeMu.Unlock()
		// A server that stopped reading (a half-open connection) mustn't hold this tunnel forever.
		_ = conn.SetWriteDeadline(time.Now().Add(30 * time.Second))
		return conn.WriteMessage(kind, data)
	}
	// The server pings every 20 s: a tunnel silent for readTimeout is gone, and ends.
	alive := func() { _ = conn.SetReadDeadline(time.Now().Add(readTimeout)) }
	alive()
	conn.SetPingHandler(func(data string) error {
		alive()
		_ = conn.WriteControl(websocket.PongMessage, []byte(data), time.Now().Add(10*time.Second))
		return nil
	})
	conn.SetPongHandler(func(string) error { alive(); return nil })
	stop := make(chan struct{})
	defer close(stop)
	// The server closes the tunnel once the token it was opened with expires, which a long reply
	// (or a kept-alive connection) outlasts: each newer token goes over it as a text frame.
	go func() {
		// If the token can't be read now (the sign-in service briefly down), keep going: the first one
		// read is sent, rather than leaving the tunnel to be closed at expiry.
		token, _ := p.Upstream.AccessToken(server)
		sent := time.Now()
		ticker := time.NewTicker(renewEvery)
		defer ticker.Stop()
		for {
			select {
			case <-stop:
				return
			case <-ticker.C:
				next, err := p.Upstream.AccessToken(server)
				// A new token at once; the current one again every resendEvery, in case the server missed it.
				if err != nil || (next == token && time.Since(sent) < resendEvery) {
					continue
				}
				message, _ := json.Marshal(map[string]string{"type": "token", "token": next})
				if write(websocket.TextMessage, message) == nil {
					token, sent = next, time.Now()
				}
			}
		}
	}()
	// Server to model server.
	go func() {
		defer func() { done <- struct{}{} }()
		for {
			kind, data, err := conn.ReadMessage()
			if err != nil {
				return
			}
			alive()
			if kind == websocket.BinaryMessage {
				if _, err := local.Write(data); err != nil {
					return
				}
				providers.Activity.TunnelIn(len(data))
			}
		}
	}()
	// Model server to server: one writer on the WebSocket, as gorilla needs.
	go func() {
		defer func() { done <- struct{}{} }()
		buffer := make([]byte, 64*1024)
		for {
			n, err := local.Read(buffer)
			if n > 0 {
				if werr := write(websocket.BinaryMessage, buffer[:n]); werr != nil {
					return
				}
				providers.Activity.TunnelOut(n)
			}
			if err != nil {
				if err == io.EOF {
					writeMu.Lock()
					_ = conn.WriteControl(websocket.CloseMessage, websocket.FormatCloseMessage(websocket.CloseNormalClosure, ""), time.Now().Add(time.Second))
					writeMu.Unlock()
				}
				return
			}
		}
	}()
	<-done
	return nil
}
