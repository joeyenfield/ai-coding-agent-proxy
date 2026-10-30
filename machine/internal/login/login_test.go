package login

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"ai-coding-agent-proxy/machine/internal/settings"
)

func TestDeviceLoginAndRevocation(t *testing.T) {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	t.Setenv(settings.TokenEnvironment, "")
	t.Setenv("AI_PROXY_URL", "")
	var issuer string
	revoked := ""
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var reply any
		switch r.URL.Path {
		case "/api/auth/status":
			reply = map[string]any{"required": true, "issuer": issuer, "client_id": "mesh-cli"}
		case "/.well-known/openid-configuration":
			reply = map[string]any{"issuer": issuer, "device_authorization_endpoint": issuer + "/device_authorization", "token_endpoint": issuer + "/token", "userinfo_endpoint": issuer + "/userinfo", "revocation_endpoint": issuer + "/revoke"}
		case "/device_authorization":
			reply = map[string]any{"device_code": "secret-device", "user_code": "ABCD", "verification_uri": issuer + "/verify", "expires_in": 120, "interval": 1}
		case "/token":
			_ = r.ParseForm()
			if r.Form.Get("device_code") != "secret-device" {
				t.Error("missing device credential")
			}
			reply = map[string]any{"access_token": "oauth-access", "refresh_token": "oauth-refresh", "expires_in": 3600, "token_type": "Bearer"}
		case "/userinfo":
			if r.Header.Get("Authorization") != "Bearer oauth-access" {
				t.Error("userinfo not signed")
			}
			reply = map[string]any{"sub": "u1", "preferred_username": "alice"}
		case "/revoke":
			_ = r.ParseForm()
			revoked = r.Form.Get("token")
			if r.Form.Get("client_id") != "mesh-cli" {
				t.Error("missing public client id")
			}
			reply = map[string]any{}
		default:
			w.WriteHeader(404)
		}
		_ = json.NewEncoder(w).Encode(reply)
	}))
	defer server.Close()
	issuer = server.URL
	if status := Login([]string{server.URL}); status != 0 {
		t.Fatalf("login: %d", status)
	}
	token, err := (settings.Upstream{}).AccessToken(server.URL)
	if err != nil || token != "oauth-access" {
		t.Fatalf("access: %q %v", token, err)
	}
	if status := Logout(nil); status != 0 || revoked != "oauth-refresh" {
		t.Fatalf("logout: %d, revoked %q", status, revoked)
	}
	if _, err := (settings.Upstream{}).AccessToken(server.URL); err == nil {
		t.Fatal("logout still authorized")
	}
}

func TestLegacyLoginIsRefused(t *testing.T) {
	for _, args := range [][]string{{"--user", "alice"}, {"--key", "old-key"}, {"-u", "alice"}} {
		if Login(args) != 2 {
			t.Fatalf("legacy option accepted: %v", args)
		}
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"required":false}`))
	}))
	defer server.Close()
	if Login([]string{server.URL}) != 1 {
		t.Fatal("local mode accepted")
	}
}
