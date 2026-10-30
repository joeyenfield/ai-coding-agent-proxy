package settings

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestConcurrentRefreshRotatesOnceAndFailuresAreVisible(t *testing.T) {
	t.Setenv("AI_PROXY_CREDENTIALS", filepath.Join(t.TempDir(), "credentials.json"))
	t.Setenv(TokenEnvironment, "")
	var exchanges atomic.Int32
	var rejected atomic.Bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		exchanges.Add(1)
		_ = r.ParseForm()
		if (r.Form.Get("refresh_token") != "refresh-one" && r.Form.Get("refresh_token") != "refresh-two") || r.Form.Get("client_id") != "mesh-cli" {
			t.Error("wrong refresh credential")
		}
		if rejected.Load() {
			w.WriteHeader(400)
			_, _ = w.Write([]byte(`{"error":"invalid_grant"}`))
			return
		}
		_, _ = w.Write([]byte(`{"access_token":"access-two","refresh_token":"refresh-two","expires_in":3600,"token_type":"Bearer"}`))
	}))
	defer server.Close()
	credentials := &Credentials{Servers: map[string]*ServerEntry{
		Normalize(server.URL): {URL: server.URL, AccessToken: "access-one", RefreshToken: "refresh-one", ExpiresAt: 1, Issuer: server.URL, ClientID: "mesh-cli", TokenEndpoint: server.URL + "/token"},
	}}
	if err := credentials.Save(); err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for i := 0; i < 10; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			token, err := (Upstream{}).AccessToken(server.URL)
			if err != nil || token != "access-two" {
				t.Errorf("refresh: %q %v", token, err)
			}
		}()
	}
	wg.Wait()
	if exchanges.Load() != 1 {
		t.Fatalf("refresh exchanges: %d", exchanges.Load())
	}
	entry := LoadCredentials().Servers[Normalize(server.URL)]
	if entry.RefreshToken != "refresh-two" {
		t.Fatal("rotation not persisted")
	}
	if err := (Upstream{}).RejectAccessToken(server.URL); err != nil {
		t.Fatal(err)
	}
	if _, err := (Upstream{}).AccessToken(server.URL); err != nil {
		t.Fatal(err)
	}
	if exchanges.Load() != 2 {
		t.Fatal("server invalidation did not force refresh before advertised expiry")
	}
	rejected.Store(true)
	credentials.Servers[Normalize(server.URL)].ExpiresAt = 1
	if err := credentials.Save(); err != nil {
		t.Fatal(err)
	}
	if token, err := (Upstream{}).AccessToken(server.URL); err == nil || token != "" {
		t.Fatal("refresh failure hidden")
	}
	if entry := LoadCredentials().Servers[Normalize(server.URL)]; entry.RefreshToken != "" || entry.AccessToken != "" {
		t.Fatal("revoked credential remains reusable")
	}
}

func TestDevicePendingSlowDownAndEndpointValidation(t *testing.T) {
	oldSleep := deviceSleep
	defer func() { deviceSleep = oldSleep }()
	var waits []time.Duration
	deviceSleep = func(wait time.Duration) { waits = append(waits, wait) }
	var issuer string
	polls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var reply any
		switch r.URL.Path {
		case "/api/auth/status":
			reply = map[string]any{"required": true, "issuer": issuer, "client_id": "mesh-cli"}
		case "/.well-known/openid-configuration":
			reply = map[string]any{"issuer": issuer, "device_authorization_endpoint": issuer + "/device_authorization", "token_endpoint": issuer + "/token", "userinfo_endpoint": issuer + "/userinfo", "revocation_endpoint": issuer + "/revoke"}
		case "/device_authorization":
			_ = r.ParseForm()
			if r.Form.Get("scope") != "openid profile offline_access mesh-api" {
				t.Error("missing offline API scope")
			}
			reply = map[string]any{"device_code": "d", "user_code": "U", "verification_uri": issuer + "/verify", "expires_in": 120, "interval": 1}
		case "/token":
			polls++
			switch polls {
			case 1:
				w.WriteHeader(400)
				reply = map[string]any{"error": "authorization_pending"}
			case 2:
				w.WriteHeader(400)
				reply = map[string]any{"error": "slow_down"}
			default:
				reply = map[string]any{"access_token": "a", "refresh_token": "r", "expires_in": 60, "token_type": "Bearer"}
			}
		case "/userinfo":
			reply = map[string]any{"sub": "u1", "username": "alice"}
		}
		_ = json.NewEncoder(w).Encode(reply)
	}))
	defer server.Close()
	issuer = server.URL
	entry, err := DeviceLogin(server.URL, &discard{})
	if err != nil || entry.User != "alice" || len(waits) != 3 || waits[2] != 6*time.Second {
		t.Fatalf("device flow: %+v %v waits %v", entry, err, waits)
	}
	if IssuerEndpoint(issuer, "https://attacker.example/token") == nil {
		t.Fatal("cross-origin endpoint allowed")
	}
	if SecureURL("http://public.example") == nil {
		t.Fatal("unencrypted API allowed")
	}
}

type discard struct{}

func (*discard) Write(p []byte) (int, error) { return len(p), nil }

func TestOAuthRedirectsAreNotFollowed(t *testing.T) {
	var leaked atomic.Bool
	destination := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { leaked.Store(true) }))
	defer destination.Close()
	redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, destination.URL, 307) }))
	defer redirect.Close()
	if _, err := TokenRequest(redirect.URL, map[string][]string{"refresh_token": {"secret"}}); err == nil {
		t.Fatal("redirect accepted")
	}
	if leaked.Load() {
		t.Fatal("credential sent to redirect")
	}
}
