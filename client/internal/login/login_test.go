package login

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"ai-coding-agent-proxy/client/internal/settings"
)

func TestLoginSavesATokenAndLogoutRevokesIt(t *testing.T) {
	t.Setenv("AI_PROXY_CREDENTIALS", t.TempDir()+"/credentials.json")
	t.Setenv("AI_PROXY_PASSWORD_INPUT", "correct horse")
	t.Setenv("AI_PROXY_TOKEN", "")
	t.Setenv("AI_PROXY_URL", "")
	revoked := ""
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/auth/status":
			_, _ = w.Write([]byte(`{"required": true}`))
		case r.URL.Path == "/api/auth/token":
			var body map[string]string
			_ = json.NewDecoder(r.Body).Decode(&body)
			if body["username"] != "alice" || body["password"] != "correct horse" {
				w.WriteHeader(http.StatusUnauthorized)
				_, _ = w.Write([]byte(`{"detail":"That username and password don't match."}`))
				return
			}
			_, _ = w.Write([]byte(`{"id":"t1","token":"aip_secret","user":"alice"}`))
		case r.Method == "DELETE" && r.URL.Path == "/api/auth/tokens/t1":
			revoked = r.Header.Get(settings.TokenHeader)
			_, _ = w.Write([]byte(`{}`))
		}
	}))
	defer server.Close()
	if status := Login([]string{server.URL, "--user", "alice"}); status != 0 {
		t.Fatalf("login: %d", status)
	}
	if got := (settings.Upstream{}).Server(); got != server.URL {
		t.Fatalf("default server %s", got)
	}
	if got := (settings.Upstream{}).Token(server.URL); got != "aip_secret" {
		t.Fatalf("token %q", got)
	}
	if status := Logout(nil); status != 0 || revoked != "aip_secret" {
		t.Fatalf("logout %d, revoked %q", status, revoked)
	}
	if got := (settings.Upstream{}).Token(server.URL); got != "" {
		t.Fatalf("token after logout %q", got)
	}
	t.Setenv("AI_PROXY_PASSWORD_INPUT", "wrong")
	if status := Login([]string{"--user", "alice", server.URL}); status != 1 {
		t.Fatalf("wrong password: %d", status)
	}
}
