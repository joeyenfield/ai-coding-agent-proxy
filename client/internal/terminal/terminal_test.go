package terminal

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func TestHistoryKeepsTheNewestBytesWithOffsets(t *testing.T) {
	history := NewHistory(10)
	history.Append([]byte("0123456789"))
	history.Append([]byte("abcd"))
	if history.End() != 14 {
		t.Fatalf("end %d", history.End())
	}
	if start, data := history.Since(0); start != 4 || string(data) != "456789abcd" {
		t.Fatalf("since 0: %d %q", start, data)
	}
	if start, data := history.Since(12); start != 12 || string(data) != "cd" {
		t.Fatalf("since 12: %d %q", start, data)
	}
}

func TestURLs(t *testing.T) {
	if got := HostURL("https://proxy.lan:8443/", "abc"); got != "wss://proxy.lan:8443/api/terminals/abc/host" {
		t.Fatal(got)
	}
	if got := ViewURL("http://server:9999/", "abc"); got != "http://server:9999/terminals/abc" {
		t.Fatal(got)
	}
}

func TestCompleteUTF8HoldsBackASplitCharacter(t *testing.T) {
	euro := []byte("€") // three bytes
	if n := completeUTF8(append([]byte("ab"), euro[:2]...)); n != 2 {
		t.Fatalf("split: %d", n)
	}
	if n := completeUTF8(append([]byte("ab"), euro...)); n != 5 {
		t.Fatalf("whole: %d", n)
	}
}

// fakeHost plays the server's /api/terminals/<id>/host end.
type fakeHost struct {
	mu       sync.Mutex
	hello    map[string]any
	output   []byte
	messages []map[string]any
	conn     *websocket.Conn
	ready    chan struct{}
}

func (f *fakeHost) record(kind int, data []byte) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if kind == websocket.BinaryMessage {
		f.output = append(f.output, data...)
		return
	}
	var message map[string]any
	_ = json.Unmarshal(data, &message)
	f.messages = append(f.messages, message)
}

func (f *fakeHost) has(test func() bool) bool {
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		f.mu.Lock()
		ok := test()
		f.mu.Unlock()
		if ok {
			return true
		}
		time.Sleep(20 * time.Millisecond)
	}
	return false
}

func TestShareStreamsHistoryTakesInputAndReportsExit(t *testing.T) {
	host := &fakeHost{ready: make(chan struct{})}
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		_, data, _ := conn.ReadMessage()
		host.mu.Lock()
		_ = json.Unmarshal(data, &host.hello)
		host.conn = conn
		host.mu.Unlock()
		_ = conn.WriteMessage(websocket.TextMessage, []byte(`{"type":"resume","from":0}`))
		close(host.ready)
		for {
			kind, data, err := conn.ReadMessage()
			if err != nil {
				return
			}
			host.record(kind, data)
		}
	}))
	defer server.Close()

	var typedMu sync.Mutex
	var typed []string
	history := NewHistory(HistoryLimit)
	history.Append([]byte("started before the connection\r\n"))
	share := NewShare(HostURL(server.URL, "s1"), "claude")
	share.Start(history, func(data []byte) {
		typedMu.Lock()
		typed = append(typed, string(data))
		typedMu.Unlock()
	}, 120, 40)
	<-host.ready
	if !host.has(func() bool { return string(host.output) == "started before the connection\r\n" }) {
		t.Fatalf("history not sent: %q", host.output)
	}
	if host.hello["cols"] != float64(120) || host.hello["command"] != "claude" {
		t.Fatalf("hello %v", host.hello)
	}
	history.Append([]byte("live output\r\n"))
	if !host.has(func() bool { return strings.HasSuffix(string(host.output), "live output\r\n") }) {
		t.Fatal("live output not sent")
	}
	// The server sends text and Enter as separate pieces; they're typed with a gap between.
	began := time.Now()
	host.mu.Lock()
	_ = host.conn.WriteMessage(websocket.TextMessage, []byte(`{"type":"input","parts":["hello","\r"]}`))
	host.mu.Unlock()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		typedMu.Lock()
		count := len(typed)
		typedMu.Unlock()
		if count >= 2 {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	typedMu.Lock()
	if strings.Join(typed, "|") != "hello|\r" || time.Since(began) < InputGap {
		t.Fatalf("typed %q in %s", typed, time.Since(began))
	}
	typedMu.Unlock()
	share.Resize(100, 30)
	if !host.has(func() bool { return len(host.messages) > 0 && host.messages[len(host.messages)-1]["type"] == "resize" }) {
		t.Fatal("resize not sent")
	}
	share.Finish(7, 5*time.Second)
	if !host.has(func() bool {
		last := host.messages[len(host.messages)-1]
		return last["type"] == "exit" && last["status"] == float64(7)
	}) {
		t.Fatalf("exit not sent: %v", host.messages)
	}
	if share.Problem() != "" {
		t.Fatalf("problem: %s", share.Problem())
	}
}

func TestShareStopsWhenTheServerRefuses(t *testing.T) {
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		// Read the hello first, as the server does: closing with it unread would reset the connection.
		_, _, _ = conn.ReadMessage()
		_ = conn.WriteControl(websocket.CloseMessage, websocket.FormatCloseMessage(4404, "Unknown session"), time.Now().Add(time.Second))
		_, _, _ = conn.ReadMessage()
	}))
	defer server.Close()
	share := NewShare(HostURL(server.URL, "missing"), "claude")
	share.Start(NewHistory(HistoryLimit), func([]byte) {}, 80, 24)
	share.Finish(0, 5*time.Second)
	if share.Problem() != "Unknown session" {
		t.Fatalf("problem %q", share.Problem())
	}
}
