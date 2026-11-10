package localproxy

import (
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gorilla/websocket"

	"ai-control-mesh/client/internal/settings"
)

func TestAnHTTPSTunnelRenewsItsTokenWhileItCarriesAConnection(t *testing.T) {
	old := renewEvery
	renewEvery = 50 * time.Millisecond
	defer func() { renewEvery = old }()
	var issued atomic.Int32
	renewed := make(chan string, 4)
	dialled := make(chan string, 4)
	upgrader := websocket.Upgrader{}
	server := newFakeServer(t, func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/token":
			// Tokens always inside RefreshBefore, so every check refreshes one.
			_, _ = fmt.Fprintf(w, `{"access_token":"access-%d","refresh_token":"refresh","expires_in":%d,"token_type":"Bearer"}`, issued.Add(1), settings.RefreshBefore+1)
			return
		case "/api/proxy/tunnel":
		default:
			http.NotFound(w, r)
			return
		}
		dialled <- r.Header.Get(settings.TokenHeader)
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		for {
			kind, data, err := conn.ReadMessage()
			if err != nil {
				return
			}
			var message map[string]string
			if kind == websocket.TextMessage && json.Unmarshal(data, &message) == nil && message["type"] == "token" {
				renewed <- message["token"]
			}
		}
	})
	proxy, _ := newProxy(t, server.URL)
	t.Setenv("AI_PROXY_TOKEN", "")
	credentials := &settings.Credentials{Servers: map[string]*settings.ServerEntry{
		settings.Normalize(server.URL): {URL: server.URL, AccessToken: "access-0", RefreshToken: "refresh", ExpiresAt: 1, Issuer: server.URL, ClientID: "mesh-cli", TokenEndpoint: server.URL + "/token"},
	}}
	if err := credentials.Save(); err != nil {
		t.Fatal(err)
	}
	listener, err := Listen(0)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	go func() { _ = proxy.ServeTunnels(listener) }()
	conn, err := net.Dial("tcp", listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	_, _ = io.WriteString(conn, "CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n")
	var first string
	select {
	case first = <-dialled:
	case <-time.After(5 * time.Second):
		t.Fatal("the tunnel never reached the server")
	}
	select {
	case token := <-renewed:
		if token == first || !strings.HasPrefix(token, "access-") {
			t.Fatalf("renewed with %q after opening with %q", token, first)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("no token sent over the open tunnel")
	}
}
