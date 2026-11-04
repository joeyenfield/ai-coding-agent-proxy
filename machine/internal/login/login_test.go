package login

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"sync"
	"testing"

	"ai-coding-agent-proxy/machine/internal/settings"
)

// fakeIssuer is the server and its issuer in one: auth status, discovery, device authorization,
// /device/setup (answering setup), tokens, userinfo and revocation.
type fakeIssuer struct {
	*httptest.Server
	mu      sync.Mutex
	setups  []url.Values
	tokens  int
	revoked string
	// setup answers /device/setup: a status and a JSON body.
	setup func() (int, any)
}

func newIssuer(t *testing.T) *fakeIssuer {
	t.Setenv("AI_PROXY_MACHINE_HOME", t.TempDir())
	t.Setenv(settings.TokenEnvironment, "")
	t.Setenv(settings.SetupCodeEnvironment, "")
	t.Setenv(EnableEnvironment, "")
	t.Setenv("AI_PROXY_URL", "")
	f := &fakeIssuer{setup: func() (int, any) { return 200, map[string]any{"approved": true, "user": "alice"} }}
	f.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		issuer := f.URL
		status, reply := 200, any(nil)
		switch r.URL.Path {
		case "/api/auth/status":
			reply = map[string]any{"required": true, "issuer": issuer, "client_id": "mesh-cli"}
		case "/.well-known/openid-configuration":
			reply = map[string]any{"issuer": issuer, "device_authorization_endpoint": issuer + "/device_authorization", "token_endpoint": issuer + "/token", "userinfo_endpoint": issuer + "/userinfo", "revocation_endpoint": issuer + "/revoke"}
		case "/device_authorization":
			reply = map[string]any{"device_code": "secret-device", "user_code": "ABCD-EFGH", "verification_uri": issuer + "/verify", "expires_in": 120, "interval": 1}
		case "/device/setup":
			_ = r.ParseForm()
			f.mu.Lock()
			f.setups = append(f.setups, r.PostForm)
			f.mu.Unlock()
			status, reply = f.setup()
		case "/token":
			_ = r.ParseForm()
			if r.Form.Get("device_code") != "secret-device" {
				t.Error("missing device credential")
			}
			f.mu.Lock()
			f.tokens++
			f.mu.Unlock()
			reply = map[string]any{"access_token": "oauth-access", "refresh_token": "oauth-refresh", "expires_in": 3600, "token_type": "Bearer"}
		case "/userinfo":
			if r.Header.Get("Authorization") != "Bearer oauth-access" {
				t.Error("userinfo not signed")
			}
			reply = map[string]any{"sub": "u1", "preferred_username": "alice"}
		case "/revoke":
			_ = r.ParseForm()
			f.revoked = r.Form.Get("token")
			if r.Form.Get("client_id") != "mesh-cli" {
				t.Error("missing public client id")
			}
			reply = map[string]any{}
		default:
			status = 404
		}
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(reply)
	}))
	t.Cleanup(f.Close)
	return f
}

// capture runs fn with stdout and stderr captured, and returns them together.
func capture(t *testing.T, fn func() int) (int, string) {
	t.Helper()
	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	stdout, stderr := os.Stdout, os.Stderr
	os.Stdout, os.Stderr = writer, writer
	var output bytes.Buffer
	done := make(chan struct{})
	go func() {
		_, _ = io.Copy(&output, reader)
		close(done)
	}()
	status := fn()
	os.Stdout, os.Stderr = stdout, stderr
	writer.Close()
	<-done
	return status, output.String()
}

func TestDeviceLoginAndRevocation(t *testing.T) {
	f := newIssuer(t)
	if status := Login([]string{f.URL}); status != 0 {
		t.Fatalf("login: %d", status)
	}
	if len(f.setups) != 0 {
		t.Fatal("a sign-in without a code called /device/setup")
	}
	token, err := (settings.Upstream{}).AccessToken(f.URL)
	if err != nil || token != "oauth-access" {
		t.Fatalf("access: %q %v", token, err)
	}
	if status := Logout(nil); status != 0 || f.revoked != "oauth-refresh" {
		t.Fatalf("logout: %d, revoked %q", status, f.revoked)
	}
	if _, err := (settings.Upstream{}).AccessToken(f.URL); err == nil {
		t.Fatal("logout still authorized")
	}
}

func TestLoginWithSetupCode(t *testing.T) {
	f := newIssuer(t)
	code := "msc_secret-setup-code"
	status, output := capture(t, func() int { return Login([]string{f.URL, "--code", code, "--enable", "launches, terminals"}) })
	if status != 0 {
		t.Fatalf("login: %d\n%s", status, output)
	}
	if strings.Contains(output, code) || strings.Contains(output, "ABCD-EFGH") {
		t.Fatalf("the setup code or user code was printed:\n%s", output)
	}
	if len(f.setups) != 1 || f.setups[0].Get("user_code") != "ABCD-EFGH" || f.setups[0].Get("setup_code") != code {
		t.Fatalf("/device/setup calls: %v", f.setups)
	}
	if f.tokens != 1 {
		t.Fatalf("token polls: %d", f.tokens)
	}
	if token, err := (settings.Upstream{}).AccessToken(f.URL); err != nil || token != "oauth-access" {
		t.Fatalf("access: %q %v", token, err)
	}
	machine := settings.LoadMachine()
	if !machine.Launches || !machine.Commands || machine.Servers || machine.Containers || machine.Updates {
		t.Fatalf("toggles: %+v", machine)
	}
}

func TestLoginWithSetupCodeFromEnvironment(t *testing.T) {
	f := newIssuer(t)
	t.Setenv(settings.SetupCodeEnvironment, "msc_from-env")
	t.Setenv(EnableEnvironment, "servers,containers")
	if status, output := capture(t, func() int { return Login([]string{f.URL}) }); status != 0 || strings.Contains(output, "msc_from-env") {
		t.Fatalf("login: %d\n%s", status, output)
	}
	if len(f.setups) != 1 || f.setups[0].Get("setup_code") != "msc_from-env" {
		t.Fatalf("/device/setup calls: %v", f.setups)
	}
	if machine := settings.LoadMachine(); !machine.Servers || !machine.Containers || machine.Launches {
		t.Fatalf("toggles: %+v", machine)
	}
}

func TestInvalidSetupCode(t *testing.T) {
	f := newIssuer(t)
	f.setup = func() (int, any) {
		return 400, map[string]any{"error": "invalid_setup_code", "error_description": "unknown"}
	}
	status, output := capture(t, func() int { return Login([]string{f.URL, "--code=msc_wrong", "--enable", "launches"}) })
	if status != 1 || !strings.Contains(output, "The setup code isn't valid: it may have expired or been used up. Create a new one on the console's Machines page.") {
		t.Fatalf("login: %d\n%s", status, output)
	}
	if strings.Contains(output, "msc_wrong") || f.tokens != 0 {
		t.Fatalf("code printed or token polled (%d):\n%s", f.tokens, output)
	}
	if _, err := os.Stat(settings.CredentialsPath()); err == nil {
		t.Fatal("credentials saved after a refused code")
	}
	if settings.LoadMachine().Launches {
		t.Fatal("toggle switched on after a refused code")
	}
	for _, answer := range []struct {
		status int
		error  string
		want   string
	}{{400, "invalid_user_code", "device code"}, {409, "code_already_used", "already approved"}, {429, "slow_down", "too many"}} {
		f.setup = func() (int, any) { return answer.status, map[string]any{"error": answer.error} }
		if status, output := capture(t, func() int { return Login([]string{f.URL, "--code", "msc_x"}) }); status != 1 || !strings.Contains(output, answer.want) {
			t.Fatalf("%s: %d\n%s", answer.error, status, output)
		}
	}
}

func TestUnknownToggleIsRefusedBeforeSigningIn(t *testing.T) {
	f := newIssuer(t)
	status, output := capture(t, func() int { return Login([]string{f.URL, "--code", "msc_x", "--enable", "launches,rockets"}) })
	if status != 2 || !strings.Contains(output, "launches, commands, updates, servers, containers") {
		t.Fatalf("login: %d\n%s", status, output)
	}
	t.Setenv(EnableEnvironment, "nope")
	if status, _ := capture(t, func() int { return Login([]string{f.URL}) }); status != 2 {
		t.Fatalf("env toggle: %d", status)
	}
	if len(f.setups) != 0 || f.tokens != 0 {
		t.Fatal("signed in despite an unknown toggle")
	}
}

func TestEnableWithDeviceLogin(t *testing.T) {
	f := newIssuer(t)
	if status, output := capture(t, func() int { return Login([]string{f.URL, "--enable", "updates"}) }); status != 0 || !strings.Contains(output, "enter code ABCD-EFGH") {
		t.Fatalf("login: %d\n%s", status, output)
	}
	if !settings.LoadMachine().Updates || len(f.setups) != 0 {
		t.Fatal("updates not switched on, or a setup code used")
	}
}

func TestAutomaticSignIn(t *testing.T) {
	f := newIssuer(t)
	// No code: nothing to do.
	if err := Automatic(f.URL); err != nil || len(f.setups) != 0 {
		t.Fatalf("without a code: %v", err)
	}
	t.Setenv(settings.SetupCodeEnvironment, "msc_auto")
	t.Setenv(EnableEnvironment, "launches")
	if _, output := capture(t, func() int {
		if err := Automatic(f.URL); err != nil {
			t.Error(err)
		}
		return 0
	}); strings.Contains(output, "msc_auto") {
		t.Fatalf("code printed:\n%s", output)
	}
	if len(f.setups) != 1 || !settings.LoadMachine().Launches {
		t.Fatalf("setups %v", f.setups)
	}
	if os.Getenv(settings.SetupCodeEnvironment) != "" {
		t.Fatal("the code stays in the environment agents inherit")
	}
	// A restart finds the credentials and leaves the code alone.
	t.Setenv(settings.SetupCodeEnvironment, "msc_auto")
	if err := Automatic(f.URL); err != nil || len(f.setups) != 1 {
		t.Fatalf("signed in again: %v, %d", err, len(f.setups))
	}
	// A refused code fails, with the message.
	other := newIssuer(t)
	other.setup = func() (int, any) { return 400, map[string]any{"error": "invalid_setup_code"} }
	t.Setenv(settings.SetupCodeEnvironment, "msc_auto")
	if err := Automatic(other.URL); err == nil || !strings.Contains(err.Error(), "Machines page") {
		t.Fatalf("refused code: %v", err)
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
