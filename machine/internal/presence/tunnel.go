package presence

import (
	"fmt"
	"io"
	"net"
	"strconv"
	"sync"
	"time"

	"github.com/gorilla/websocket"
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
	done := make(chan struct{}, 2)
	// Server to model server.
	go func() {
		defer func() { done <- struct{}{} }()
		for {
			kind, data, err := conn.ReadMessage()
			if err != nil {
				return
			}
			if kind == websocket.BinaryMessage {
				if _, err := local.Write(data); err != nil {
					return
				}
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
				if werr := conn.WriteMessage(websocket.BinaryMessage, buffer[:n]); werr != nil {
					return
				}
			}
			if err != nil {
				if err == io.EOF {
					_ = conn.WriteControl(websocket.CloseMessage, websocket.FormatCloseMessage(websocket.CloseNormalClosure, ""), time.Now().Add(time.Second))
				}
				return
			}
		}
	}()
	<-done
	return nil
}
