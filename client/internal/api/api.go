// Package api makes the launcher's JSON calls to the server, through this machine's
// agent-proxy (which signs them).
package api

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	"ai-coding-agent-proxy/client/internal/settings"
)

// Error is an answer of 400 or more, with the server's own message.
type Error struct {
	Status int
	Detail string
}

func (e *Error) Error() string { return e.Detail }

// StatusOf is the HTTP status of an *Error, or 0.
func StatusOf(err error) int {
	if apiError, ok := err.(*Error); ok {
		return apiError.Status
	}
	return 0
}

// Client calls one base URL (the agent-proxy's) with JSON in and out.
type Client struct {
	Base string
	HTTP *http.Client
}

func New(base string, timeout time.Duration) *Client {
	return &Client{Base: base, HTTP: settings.ServerClient(timeout)}
}

// Do sends body as JSON (when not nil) and decodes the answer into out (when not nil).
func (c *Client) Do(method, path string, body, out any) error {
	var reader io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(encoded)
	}
	request, err := http.NewRequest(method, c.Base+path, reader)
	if err != nil {
		return err
	}
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := c.HTTP.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	data, err := io.ReadAll(response.Body)
	if err != nil {
		return err
	}
	if response.StatusCode >= 400 {
		return &Error{Status: response.StatusCode, Detail: settings.Detail(data)}
	}
	if out != nil {
		if err := json.Unmarshal(data, out); err != nil {
			return fmt.Errorf("%s %s: unexpected answer: %w", method, path, err)
		}
	}
	return nil
}

func (c *Client) Get(path string, out any) error { return c.Do(http.MethodGet, path, nil, out) }

func (c *Client) Post(path string, body, out any) error {
	return c.Do(http.MethodPost, path, body, out)
}

func (c *Client) Patch(path string, body, out any) error {
	return c.Do(http.MethodPatch, path, body, out)
}

// Text fetches a plain-text answer, such as a PEM file.
func (c *Client) Text(path string) (string, error) {
	response, err := c.HTTP.Get(c.Base + path)
	if err != nil {
		return "", err
	}
	defer response.Body.Close()
	data, err := io.ReadAll(response.Body)
	if err != nil {
		return "", err
	}
	if response.StatusCode >= 400 {
		return "", &Error{Status: response.StatusCode, Detail: settings.Detail(data)}
	}
	return string(data), nil
}

// Health is GET /health of any of the programs, or nil when it doesn't answer.
func Health(base string) map[string]any {
	response, err := settings.Client(750 * time.Millisecond).Get(base + "/health")
	if err != nil {
		return nil
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil
	}
	var body map[string]any
	if json.NewDecoder(response.Body).Decode(&body) != nil {
		return nil
	}
	return body
}
