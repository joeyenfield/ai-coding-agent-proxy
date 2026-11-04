package settings

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"
)

// A sign-in service that's down or slow while the access token still has a while left: the stored
// token is used, the refresh isn't tried again at once (it may have been consumed), and it's tried
// again after RefreshRetry. Only a refused refresh means signing in again.
func TestAFailedRefreshKeepsAValidTokenAndBacksOff(t *testing.T) {
	t.Setenv("AI_PROXY_CREDENTIALS", filepath.Join(t.TempDir(), "credentials.json"))
	t.Setenv(TokenEnvironment, "")
	oldRetry := RefreshRetry
	RefreshRetry = 200 * time.Millisecond
	defer func() { RefreshRetry = oldRetry }()
	var exchanges atomic.Int32
	var down atomic.Bool
	down.Store(true)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		exchanges.Add(1)
		if down.Load() {
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte(`{"detail":"unavailable"}`))
			return
		}
		_, _ = w.Write([]byte(`{"access_token":"access-two","refresh_token":"refresh-two","expires_in":3600,"token_type":"Bearer"}`))
	}))
	defer server.Close()
	key := Normalize(server.URL)
	credentials := &Credentials{Servers: map[string]*ServerEntry{
		key: {URL: server.URL, AccessToken: "access-one", RefreshToken: "refresh-one", ExpiresAt: time.Now().Unix() + 90, Issuer: server.URL, ClientID: "mesh-cli", TokenEndpoint: server.URL + "/token"},
	}}
	if err := credentials.Save(); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 5; i++ {
		token, err := (Upstream{}).AccessToken(server.URL)
		if err != nil || token != "access-one" {
			t.Fatalf("while the issuer is down: %q %v", token, err)
		}
	}
	if exchanges.Load() != 1 {
		t.Fatalf("refresh tried %d times inside the back-off", exchanges.Load())
	}
	down.Store(false)
	time.Sleep(250 * time.Millisecond)
	if token, err := (Upstream{}).AccessToken(server.URL); err != nil || token != "access-two" {
		t.Fatalf("after the back-off: %q %v", token, err)
	}
	if LoadCredentials().Servers[key].RefreshToken != "refresh-two" {
		t.Fatal("rotation not persisted")
	}

	// Expired, and the issuer still down: an error, but not one that asks for a new sign-in.
	down.Store(true)
	credentials = LoadCredentials()
	credentials.Servers[key].ExpiresAt = 1
	if err := credentials.Save(); err != nil {
		t.Fatal(err)
	}
	noteRefresh(key, false)
	_, err := (Upstream{}).AccessToken(server.URL)
	if err == nil || errors.Is(err, ErrLoginRequired) {
		t.Fatalf("expired token with the issuer down: %v", err)
	}
}
