package providers

import (
	"sync/atomic"
	"time"
)

// Activity counts what this agent-machine has done since it started, for the sample's "agent"
// part. Package presence, which holds the fleet connection and the tunnels, reports into it:
// it imports this package, so the counters live here instead of the other way round.
var Activity activity

type activity struct {
	// Unix nanoseconds the current fleet connection was welcomed; 0 while not connected.
	connectedSince atomic.Int64
	connects       atomic.Int64
	requests       atomic.Int64
	tunnelsOpen    atomic.Int64
	tunnelsTotal   atomic.Int64
	tunnelBytesIn  atomic.Int64
	tunnelBytesOut atomic.Int64
}

// Connected is called when the server welcomes a fleet connection.
func (a *activity) Connected() {
	a.connectedSince.Store(time.Now().UnixNano())
	a.connects.Add(1)
}

// Disconnected is called when that connection ends.
func (a *activity) Disconnected() { a.connectedSince.Store(0) }

// Request is called for each fleet request the machine handles.
func (a *activity) Request() { a.requests.Add(1) }

// TunnelOpened is called once a tunnel reaches its model server; TunnelClosed when it ends.
func (a *activity) TunnelOpened() {
	a.tunnelsOpen.Add(1)
	a.tunnelsTotal.Add(1)
}

func (a *activity) TunnelClosed() { a.tunnelsOpen.Add(-1) }

// TunnelIn counts bytes from the server into a model server; TunnelOut the bytes back.
func (a *activity) TunnelIn(n int) { a.tunnelBytesIn.Add(int64(n)) }

func (a *activity) TunnelOut(n int) { a.tunnelBytesOut.Add(int64(n)) }

// Snapshot is the counters as the sample's "agent" part.
func (a *activity) Snapshot() map[string]any {
	var since any
	if at := a.connectedSince.Load(); at != 0 {
		since = float64(at) / 1e9
	}
	return map[string]any{
		"connected_since": since, "connects": a.connects.Load(), "requests": a.requests.Load(),
		"tunnels_open": a.tunnelsOpen.Load(), "tunnels_total": a.tunnelsTotal.Load(),
		"tunnel_bytes_in": a.tunnelBytesIn.Load(), "tunnel_bytes_out": a.tunnelBytesOut.Load(),
	}
}
