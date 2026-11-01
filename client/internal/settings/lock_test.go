package settings

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestALockLeftByADeadProcessIsTakenOverAndOnlyTheRefusedTokenIsRejected(t *testing.T) {
	t.Setenv("AI_PROXY_CREDENTIALS", filepath.Join(t.TempDir(), "credentials.json"))
	t.Setenv(TokenEnvironment, "")
	// A lock some killed process left behind, long ago: taken over at once, not waited on.
	lock := CredentialsPath() + ".lock"
	if err := os.MkdirAll(lock, 0o700); err != nil {
		t.Fatal(err)
	}
	old := time.Now().Add(-2 * StaleLock)
	if err := os.Chtimes(lock, old, old); err != nil {
		t.Fatal(err)
	}
	started := time.Now()
	unlock, err := LockCredentials()
	if err != nil || time.Since(started) > 5*time.Second {
		t.Fatalf("stale lock: %v after %v", err, time.Since(started))
	}
	unlock()
	server := "https://server.example"
	credentials := &Credentials{Servers: map[string]*ServerEntry{
		Normalize(server): {URL: server, AccessToken: "new", RefreshToken: "r", ExpiresAt: time.Now().Unix() + 3600, Issuer: server, ClientID: "c"},
	}}
	if err := credentials.Save(); err != nil {
		t.Fatal(err)
	}
	// A socket opened with an older token was closed for it: the newer one another socket refreshed stays.
	if err := (Upstream{}).RejectAccessTokenIf(server, "old"); err != nil {
		t.Fatal(err)
	}
	if LoadCredentials().Servers[Normalize(server)].ExpiresAt == 0 {
		t.Fatal("a newer token was thrown away")
	}
	if err := (Upstream{}).RejectAccessTokenIf(server, "new"); err != nil {
		t.Fatal(err)
	}
	if LoadCredentials().Servers[Normalize(server)].ExpiresAt != 0 {
		t.Fatal("the refused token is still used")
	}
}
