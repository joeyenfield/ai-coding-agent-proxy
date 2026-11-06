package settings

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"
)

const TokenEnvironment = "AI_PROXY_TOKEN"

var deviceSleep = time.Sleep

var ErrLoginRequired = errors.New("OAuth sign-in required: run login (legacy passwords and access keys are no longer supported)")

// OAuthClient never follows redirects: a token must stay at its validated endpoint.
func OAuthClient() *http.Client {
	return &http.Client{Transport: Transport(), Timeout: 20 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
}

func SecureURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" || u.User != nil || u.Fragment != "" || u.RawQuery != "" ||
		(u.Scheme != "https" && !(u.Scheme == "http" && IsLoopbackURL(raw))) {
		return errors.New("OAuth requires HTTPS (HTTP is allowed only on loopback)")
	}
	return nil
}

func IssuerEndpoint(issuer, endpoint string) error {
	if err := SecureURL(issuer); err != nil {
		return err
	}
	if err := SecureURL(endpoint); err != nil {
		return err
	}
	a, _ := url.Parse(issuer)
	b, _ := url.Parse(endpoint)
	if !strings.EqualFold(a.Scheme, b.Scheme) || !strings.EqualFold(a.Host, b.Host) {
		return errors.New("OAuth endpoint is outside the issuer origin")
	}
	return nil
}

type Discovery struct {
	Issuer   string `json:"issuer"`
	Device   string `json:"device_authorization_endpoint"`
	Token    string `json:"token_endpoint"`
	UserInfo string `json:"userinfo_endpoint"`
	Revoke   string `json:"revocation_endpoint"`
}

func Discover(issuer string) (*Discovery, error) {
	if err := SecureURL(issuer); err != nil {
		return nil, err
	}
	response, err := OAuthClient().Get(strings.TrimRight(issuer, "/") + "/.well-known/openid-configuration")
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.StatusCode != 200 {
		return nil, fmt.Errorf("OIDC discovery returned HTTP %d", response.StatusCode)
	}
	var d Discovery
	if err := json.NewDecoder(response.Body).Decode(&d); err != nil {
		return nil, err
	}
	if d.Issuer != issuer {
		return nil, errors.New("OIDC discovery issuer mismatch")
	}
	for _, endpoint := range []string{d.Device, d.Token, d.UserInfo, d.Revoke} {
		if err := IssuerEndpoint(issuer, endpoint); err != nil {
			return nil, err
		}
	}
	return &d, nil
}

type OAuthToken struct {
	Access  string `json:"access_token"`
	Refresh string `json:"refresh_token"`
	Type    string `json:"token_type"`
	Expires int64  `json:"expires_in"`
	Error   string `json:"error"`
}

func TokenRequest(endpoint string, form url.Values) (*OAuthToken, error) {
	response, err := OAuthClient().PostForm(endpoint, form)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	var token OAuthToken
	if err := json.NewDecoder(io.LimitReader(response.Body, 1<<20)).Decode(&token); err != nil {
		return nil, fmt.Errorf("invalid OAuth response (HTTP %d)", response.StatusCode)
	}
	if token.Error != "" {
		return &token, fmt.Errorf("OAuth: %s", token.Error)
	}
	if response.StatusCode != 200 || token.Access == "" || !strings.EqualFold(token.Type, "Bearer") || token.Expires <= 0 {
		return nil, fmt.Errorf("invalid OAuth token response (HTTP %d)", response.StatusCode)
	}
	return &token, nil
}

func (entry *ServerEntry) SetToken(token *OAuthToken) {
	entry.AccessToken = token.Access
	if token.Refresh != "" {
		entry.RefreshToken = token.Refresh
	}
	entry.ExpiresAt = time.Now().Unix() + token.Expires
	entry.Token, entry.TokenID = "", ""
}

// RefreshBefore is how long before its expiry an access token is refreshed: long enough that a
// refresh on a slow or dropping network (it may take 20 s, and be tried again) still finishes before
// the server stops accepting the old token and closes the sockets it opened.
const RefreshBefore = 120

// StillValid is the margin an access token needs left to be used after a refresh failed: the
// server's own checks (every 15 s, its answers cached 10 s) must still find it good.
const StillValid = 30

// RefreshRetry is how long a process waits after a refresh the issuer couldn't answer before it
// tries again, using the token it has meanwhile. Retrying at once (every request and every socket's
// renewal tick asks) could present a refresh token the issuer did consume several times over, and
// the issuer revokes the whole sign-in on a second reuse.
var RefreshRetry = 15 * time.Second

var refreshFailed = struct {
	sync.Mutex
	at map[string]time.Time
}{at: map[string]time.Time{}}

func backingOff(server string) bool {
	refreshFailed.Lock()
	defer refreshFailed.Unlock()
	at, ok := refreshFailed.at[server]
	return ok && time.Since(at) < RefreshRetry
}

func noteRefresh(server string, failed bool) {
	refreshFailed.Lock()
	defer refreshFailed.Unlock()
	if failed {
		refreshFailed.at[server] = time.Now()
	} else {
		delete(refreshFailed.at, server)
	}
}

// validToken is the stored access token if it has StillValid seconds left, else "".
func validToken(entry *ServerEntry) string {
	if entry != nil && entry.AccessToken != "" && entry.ExpiresAt > time.Now().Unix()+StillValid {
		return entry.AccessToken
	}
	return ""
}

// StaleLock is how old a credentials lock must be before it's taken for one a killed process left
// behind. Whoever holds it does one read, one refresh (at most 20 s) and one write, so a lock this old
// has no live owner; without this, every later sign-in and request would wait and fail until someone
// deleted it by hand (Android kills background processes, and an update stops the old one).
const StaleLock = 2 * time.Minute

// LockCredentials serializes the whole read/refresh/save transaction across Go and Python.
// A timed-out lock isn't stolen (that could reuse a rotating refresh token), only one past StaleLock.
func LockCredentials() (func(), error) {
	path := CredentialsPath() + ".lock"
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	deadline := time.Now().Add(30 * time.Second)
	for {
		err := os.Mkdir(path, 0o700)
		if err == nil {
			return func() { _ = os.Remove(path) }, nil
		}
		if info, statErr := os.Stat(path); statErr == nil && time.Since(info.ModTime()) > StaleLock {
			// Left by a process that died holding it: take it over. Rename it aside first,
			// so of two waiters that both saw it stale only one wins; removing in place
			// could delete the other waiter's fresh lock and let both hold it at once.
			stale := fmt.Sprintf("%s.stale-%d", path, time.Now().UnixNano())
			if os.Rename(path, stale) == nil {
				_ = os.Remove(stale)
			}
			// A remove an antivirus blocked for a moment leaves a .stale-* behind;
			// sweep leftovers here, the only place that makes them.
			if leftovers, _ := filepath.Glob(path + ".stale-*"); leftovers != nil {
				for _, leftover := range leftovers {
					_ = os.Remove(leftover)
				}
			}
			continue
		}
		// On Windows, creating the directory while another process is removing it is refused
		// as access denied, not as already existing: that's the lock being released, so wait.
		releasing := runtime.GOOS == "windows" && os.IsPermission(err)
		if !os.IsExist(err) && !releasing {
			return nil, err
		}
		if time.Now().After(deadline) {
			if releasing {
				return nil, err
			}
			return nil, errors.New("credentials are locked; another sign-in or refresh is in progress (remove a stale credentials.json.lock only after stopping clients)")
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func (u Upstream) AccessToken(server string) (string, error) {
	if err := SecureURL(server); err != nil {
		return "", err
	}
	if token := os.Getenv(TokenEnvironment); token != "" {
		return token, nil
	}
	key := Normalize(server)
	unlock, err := LockCredentials()
	if err != nil {
		// Another process holds the lock (a slow refresh): the stored token, while it's still good,
		// rather than failing the request. The file is replaced whole, so reading it unlocked is safe.
		if token := validToken(LoadCredentials().Servers[key]); token != "" {
			return token, nil
		}
		return "", err
	}
	defer unlock()
	credentials := LoadCredentials()
	entry := credentials.Servers[key]
	if entry == nil || entry.AccessToken == "" || entry.Issuer == "" || entry.ClientID == "" {
		return "", ErrLoginRequired
	}
	if entry.ExpiresAt > time.Now().Unix()+RefreshBefore {
		return entry.AccessToken, nil
	}
	if token := validToken(entry); token != "" && backingOff(key) {
		return token, nil
	}
	if entry.RefreshToken == "" {
		return "", fmt.Errorf("OAuth access token expired: %w", ErrLoginRequired)
	}
	if err := IssuerEndpoint(entry.Issuer, entry.TokenEndpoint); err != nil {
		return "", err
	}
	token, err := TokenRequest(entry.TokenEndpoint, url.Values{
		"grant_type": {"refresh_token"}, "refresh_token": {entry.RefreshToken}, "client_id": {entry.ClientID},
	})
	if err != nil {
		if token != nil && token.Error == "invalid_grant" {
			entry.AccessToken, entry.RefreshToken, entry.ExpiresAt = "", "", 0
			if saveErr := credentials.Save(); saveErr != nil {
				return "", fmt.Errorf("refresh rejected; couldn't clear credentials: %w", saveErr)
			}
			return "", fmt.Errorf("OAuth refresh revoked: %w", ErrLoginRequired)
		}
		// The issuer couldn't answer (down, restarting, slow): carry on with the token while it lasts.
		noteRefresh(key, true)
		if token := validToken(entry); token != "" {
			return token, nil
		}
		return "", fmt.Errorf("OAuth refresh failed: %w", err)
	}
	noteRefresh(key, false)
	entry.SetToken(token)
	// The old refresh token is spent: losing the new one would mean presenting the old again, which the
	// issuer allows only once, shortly after. On Windows a reader holding the file can make the replace
	// fail for a moment, so try a few times.
	for attempt := 0; ; attempt++ {
		err := credentials.Save()
		if err == nil {
			break
		}
		if attempt == 9 {
			// The token works now; the next refresh will say if the old one was refused.
			fmt.Fprintln(os.Stderr, "warning: couldn't save the renewed OAuth credential:", err)
			return entry.AccessToken, nil
		}
		time.Sleep(200 * time.Millisecond)
	}
	return entry.AccessToken, nil
}

// RejectAccessToken forces the next call to refresh after a server invalidates a
// credential before its advertised expiry. It never exchanges credentials unlocked.
func (u Upstream) RejectAccessToken(server string) error {
	return u.RejectAccessTokenIf(server, "")
}

// RejectAccessTokenIf is RejectAccessToken for the token a socket was opened with: only while it's
// still the stored one. Several sockets opened with one token are closed together when it expires;
// the first refreshes, and the others mustn't throw that new token away and refresh again each.
func (u Upstream) RejectAccessTokenIf(server, token string) error {
	if os.Getenv(TokenEnvironment) != "" {
		return nil
	}
	unlock, err := LockCredentials()
	if err != nil {
		return err
	}
	defer unlock()
	credentials := LoadCredentials()
	entry := credentials.Servers[Normalize(server)]
	if entry == nil || (token != "" && entry.AccessToken != token) {
		return nil
	}
	entry.ExpiresAt = 0
	return credentials.Save()
}

// SetupCodeEnvironment holds a setup code from the console for a sign-in without a browser: what a
// container is given, since nobody is there to approve a device code.
const SetupCodeEnvironment = "AI_PROXY_SETUP_CODE"

// ErrInvalidSetupCode is the issuer refusing the setup code itself, which only a new one fixes.
var ErrInvalidSetupCode = errors.New("The setup code isn't valid: it may have expired or been used up. Create a new one on the console's Machines page.")

// ApproveWithSetupCode approves the device authorization userCode as the setup code's owner, at
// the issuer's /device/setup. Its errors never contain the setup code.
func ApproveWithSetupCode(issuer, userCode, setupCode string) error {
	endpoint := strings.TrimRight(issuer, "/") + "/device/setup"
	if err := IssuerEndpoint(issuer, endpoint); err != nil {
		return err
	}
	response, err := OAuthClient().PostForm(endpoint, url.Values{"user_code": {userCode}, "setup_code": {setupCode}})
	if err != nil {
		// A transport error names the URL, never the form, so it can't carry the code.
		return err
	}
	defer response.Body.Close()
	var answer struct {
		Approved    bool   `json:"approved"`
		Error       string `json:"error"`
		Description string `json:"error_description"`
	}
	_ = json.NewDecoder(io.LimitReader(response.Body, 1<<20)).Decode(&answer)
	return setupCodeError(response.StatusCode, answer.Approved, answer.Error, answer.Description)
}

// setupCodeError says what an answer from /device/setup means for the person signing in.
func setupCodeError(status int, approved bool, code, description string) error {
	switch {
	case status == 200 && approved:
		return nil
	case code == "invalid_setup_code":
		return ErrInvalidSetupCode
	case code == "invalid_user_code":
		return errors.New("the issuer didn't accept this sign-in's device code (it may have expired); run login again")
	case status == 409 || code == "code_already_used":
		return errors.New("this sign-in's device code was already approved or used; run login again")
	case status == 429:
		return errors.New("too many setup code attempts from this address; wait a few minutes and try again")
	case code != "" && description != "":
		return fmt.Errorf("setup code sign-in failed: %s (%s)", code, description)
	case code != "":
		return fmt.Errorf("setup code sign-in failed: %s", code)
	}
	return fmt.Errorf("setup code sign-in failed (HTTP %d)", status)
}

// DeviceLogin signs in with OAuth device authorization: approved by a person in a browser, or by
// setupCode when it isn't empty.
func DeviceLogin(server, setupCode string, output io.Writer) (*ServerEntry, error) {
	if err := SecureURL(server); err != nil {
		return nil, err
	}
	response, err := ServerClient(20 * time.Second).Get(server + "/api/auth/status")
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	var status struct {
		Required bool   `json:"required"`
		Issuer   string `json:"issuer"`
		ClientID string `json:"client_id"`
	}
	if response.StatusCode != 200 {
		return nil, fmt.Errorf("auth status returned HTTP %d", response.StatusCode)
	}
	if err := json.NewDecoder(response.Body).Decode(&status); err != nil {
		return nil, err
	}
	if !status.Required || status.Issuer == "" || status.ClientID == "" {
		return nil, errors.New("server must require OIDC; legacy local/password/key sign-in is unsupported")
	}
	d, err := Discover(status.Issuer)
	if err != nil {
		return nil, err
	}
	deviceResponse, err := OAuthClient().PostForm(d.Device, url.Values{"client_id": {status.ClientID}, "scope": {"openid profile offline_access mesh-api"}})
	if err != nil {
		return nil, err
	}
	defer deviceResponse.Body.Close()
	var device struct {
		Code     string `json:"device_code"`
		UserCode string `json:"user_code"`
		URI      string `json:"verification_uri"`
		Complete string `json:"verification_uri_complete"`
		Expires  int64  `json:"expires_in"`
		Interval int64  `json:"interval"`
	}
	if deviceResponse.StatusCode != 200 {
		return nil, fmt.Errorf("device authorization returned HTTP %d", deviceResponse.StatusCode)
	}
	if err := json.NewDecoder(deviceResponse.Body).Decode(&device); err != nil {
		return nil, err
	}
	if device.Code == "" || device.UserCode == "" || device.Expires <= 0 {
		return nil, errors.New("invalid OAuth device authorization response")
	}
	// Verification links may contain a code in their query, but must remain at the issuer.
	verify, err := url.Parse(device.URI)
	if err != nil {
		return nil, errors.New("invalid verification URI")
	}
	verify.RawQuery = ""
	if err := IssuerEndpoint(status.Issuer, verify.String()); err != nil {
		return nil, err
	}
	approved := false
	if setupCode != "" {
		// The setup code stands in for the person at the browser; it's a bearer secret, so it's never printed.
		if err := ApproveWithSetupCode(status.Issuer, device.UserCode, setupCode); err != nil {
			return nil, err
		}
		fmt.Fprintln(output, "Approved with a setup code.")
		approved = true
	} else {
		fmt.Fprintf(output, "Open %s and enter code %s to sign in.\n", device.URI, device.UserCode)
	}
	if device.Interval <= 0 {
		device.Interval = 5
	}
	deadline := time.Now().Add(time.Duration(device.Expires) * time.Second)
	for {
		wait := time.Duration(device.Interval) * time.Second
		if time.Now().Add(wait).After(deadline) {
			return nil, errors.New("OAuth device authorization expired; run login again")
		}
		// Once a setup code has approved it the first poll succeeds: no need to wait for it.
		if approved {
			approved = false
		} else {
			deviceSleep(wait)
		}
		token, err := TokenRequest(d.Token, url.Values{"grant_type": {"urn:ietf:params:oauth:grant-type:device_code"}, "device_code": {device.Code}, "client_id": {status.ClientID}})
		if err != nil {
			if token != nil && token.Error == "authorization_pending" {
				continue
			}
			if token != nil && token.Error == "slow_down" {
				device.Interval += 5
				continue
			}
			return nil, err
		}
		entry := &ServerEntry{URL: server, Issuer: status.Issuer, ClientID: status.ClientID, TokenEndpoint: d.Token, RevocationEndpoint: d.Revoke}
		entry.SetToken(token)
		request, _ := http.NewRequest(http.MethodGet, d.UserInfo, nil)
		request.Header.Set("Authorization", "Bearer "+token.Access)
		info, err := OAuthClient().Do(request)
		if err != nil {
			return nil, err
		}
		var user struct {
			Sub       string `json:"sub"`
			Preferred string `json:"preferred_username"`
			Username  string `json:"username"`
		}
		err = json.NewDecoder(info.Body).Decode(&user)
		info.Body.Close()
		if err != nil || info.StatusCode != 200 || user.Sub == "" {
			return nil, errors.New("OAuth userinfo failed")
		}
		entry.User = user.Preferred
		if entry.User == "" {
			entry.User = user.Username
		}
		if entry.User == "" {
			entry.User = user.Sub
		}
		return entry, nil
	}
}

func Revoke(entry *ServerEntry) error {
	if entry.RefreshToken == "" {
		return nil
	}
	if err := IssuerEndpoint(entry.Issuer, entry.RevocationEndpoint); err != nil {
		return err
	}
	response, err := OAuthClient().PostForm(entry.RevocationEndpoint, url.Values{"token": {entry.RefreshToken}, "token_type_hint": {"refresh_token"}, "client_id": {entry.ClientID}})
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != 200 {
		return fmt.Errorf("OAuth revocation returned HTTP %d", response.StatusCode)
	}
	return nil
}
