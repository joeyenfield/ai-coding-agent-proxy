package presence

import (
	"bufio"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

// echoServer is a model server stand-in: it answers each line with "echo: <line>".
func echoServer(t *testing.T) int {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { listener.Close() })
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			go func(conn net.Conn) {
				defer conn.Close()
				scanner := bufio.NewScanner(conn)
				for scanner.Scan() {
					_, _ = conn.Write([]byte("echo: " + scanner.Text() + "\n"))
				}
			}(conn)
		}
	}()
	return listener.Addr().(*net.TCPAddr).Port
}

// tunnelServer plays the server's /api/machines/tunnel/<id>: it sends one line and hands back what returns.
func tunnelServer(t *testing.T, got chan<- string) *httptest.Server {
	t.Helper()
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/machines/tunnel/t1" {
			http.NotFound(w, r)
			return
		}
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		_ = conn.WriteMessage(websocket.BinaryMessage, []byte("hello model\n"))
		_ = conn.SetReadDeadline(time.Now().Add(5 * time.Second))
		_, data, err := conn.ReadMessage()
		if err == nil {
			got <- string(data)
		}
	}))
	t.Cleanup(server.Close)
	return server
}

func testPresence(server *httptest.Server) *Presence {
	return &Presence{Dial: func(base, path string) (*websocket.Conn, *http.Response, error) {
		return websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(base, "http")+path, nil)
	}}
}

func TestATunnelCarriesBytesToTheModelServerAndBack(t *testing.T) {
	port := echoServer(t)
	got := make(chan string, 1)
	server := tunnelServer(t, got)
	p := testPresence(server)
	p.tunnels.set([]int{port})
	done := make(chan error, 1)
	go func() { done <- p.tunnel(server.URL, Tunnel{Type: "tunnel", ID: "t1", Port: port}, nil) }()
	select {
	case line := <-got:
		if line != "echo: hello model\n" {
			t.Fatalf("got %q back", line)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("nothing came back through the tunnel")
	}
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("the tunnel didn't end when the server closed it")
	}
}

func TestATunnelOnlyReachesThisMachinesModelServers(t *testing.T) {
	port := echoServer(t)
	got := make(chan string, 1)
	server := tunnelServer(t, got)
	p := testPresence(server)
	p.tunnels.set([]int{11434})
	if err := p.tunnel(server.URL, Tunnel{ID: "t1", Port: port}, nil); err == nil || !strings.Contains(err.Error(), "isn't one of this machine's model servers") {
		t.Fatalf("a port it doesn't serve models on: %v", err)
	}
	// A container's port the server asked about counts too.
	go func() { _ = p.tunnel(server.URL, Tunnel{ID: "t1", Port: port}, []int{port}) }()
	select {
	case line := <-got:
		if line != "echo: hello model\n" {
			t.Fatalf("got %q back", line)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("a container's port was refused")
	}
}
