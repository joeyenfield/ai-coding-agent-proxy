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
	"strings"
	"time"
)

const TokenEnvironment = "AI_PROXY_MACHINE_TOKEN"

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

// LockCredentials serializes the whole read/refresh/save transaction across Go and Python.
// Never steal a timed-out lock: doing so could reuse a rotating refresh token.
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
		if !os.IsExist(err) {
			return nil, err
		}
		if time.Now().After(deadline) {
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
	unlock, err := LockCredentials()
	if err != nil {
		return "", err
	}
	defer unlock()
	credentials := LoadCredentials()
	entry := credentials.Servers[Normalize(server)]
	if entry == nil || entry.AccessToken == "" || entry.Issuer == "" || entry.ClientID == "" {
		return "", ErrLoginRequired
	}
	if entry.ExpiresAt > time.Now().Unix()+30 {
		return entry.AccessToken, nil
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
		return "", fmt.Errorf("OAuth refresh failed; run login if revoked: %w", err)
	}
	entry.SetToken(token)
	if err := credentials.Save(); err != nil {
		return "", fmt.Errorf("couldn't persist rotated OAuth credential: %w", err)
	}
	return entry.AccessToken, nil
}

// RejectAccessToken forces the next call to refresh after a server invalidates a
// credential before its advertised expiry. It never exchanges credentials unlocked.
func (u Upstream) RejectAccessToken(server string) error {
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
	if entry == nil {
		return nil
	}
	entry.ExpiresAt = 0
	return credentials.Save()
}

func DeviceLogin(server string, output io.Writer) (*ServerEntry, error) {
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
	fmt.Fprintf(output, "Open %s and enter code %s to sign in.\n", device.URI, device.UserCode)
	if device.Interval <= 0 {
		device.Interval = 5
	}
	deadline := time.Now().Add(time.Duration(device.Expires) * time.Second)
	for {
		wait := time.Duration(device.Interval) * time.Second
		if time.Now().Add(wait).After(deadline) {
			return nil, errors.New("OAuth device authorization expired; run login again")
		}
		deviceSleep(wait)
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
