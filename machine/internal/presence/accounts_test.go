package presence

import (
	"sync/atomic"
	"testing"
	"time"
)

func TestProviderSignInsAreLookedAtInTheBackgroundAndKept(t *testing.T) {
	var looks atomic.Int32
	cache := providerCLIs{detect: func() map[string]any {
		looks.Add(1)
		return map[string]any{"clis": []any{map[string]any{"provider": "codex", "installed": true, "auth": "signed_in"}}, "at": 1.0}
	}}
	// The first hello doesn't wait for the look.
	if first := cache.Report(); len(first["clis"].([]any)) != 0 {
		t.Fatalf("first %v", first)
	}
	deadline := time.Now().Add(5 * time.Second)
	for len(cache.Report()["clis"].([]any)) == 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if clis := cache.Report()["clis"].([]any); len(clis) != 1 {
		t.Fatalf("after the look %v", clis)
	}
	// Kept until it's due again.
	cache.Report()
	if looks.Load() != 1 {
		t.Fatalf("looked %d times", looks.Load())
	}
}

func TestTheReportIsTheAgentsLastLine(t *testing.T) {
	if line := lastLine("warning: something\r\n{\"clis\": []}\r\n"); line != "{\"clis\": []}" {
		t.Fatalf("%q", line)
	}
}
