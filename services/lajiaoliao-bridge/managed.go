package main

import (
	"bufio"
	"context"
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"strings"
	"time"
)

const bridgeProtocolVersion = 2

type managedConfig struct {
	Secret        string `json:"secret"`
	LoginKey      string `json:"login_key"`
	TokenPath     string `json:"token_path"`
	Connect       bool   `json:"connect"`
	DirectoryOnly bool   `json:"directory_only"`
	ExpiresAt     int64  `json:"expires_at"`
}

// The open stdin pipe is also the parent liveness lease. No credentials go in argv.
func runManaged() error {
	reader := bufio.NewReader(os.Stdin)
	line, err := reader.ReadString('\n')
	if err != nil || len(line) > 8192 {
		return fmt.Errorf("invalid_managed_config")
	}
	var config managedConfig
	if json.Unmarshal([]byte(line), &config) != nil || len(config.Secret) < 32 || config.TokenPath == "" {
		return fmt.Errorf("invalid_managed_config")
	}
	if config.DirectoryOnly && config.Connect {
		return fmt.Errorf("invalid_managed_config")
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	s := &server{tokens: newTokenStore(config.TokenPath)}
	s.upstream = &openIM{store: s.tokens}
	if config.Connect {
		go s.upstream.run(ctx)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return fmt.Errorf("managed_listen_failed")
	}
	httpServer := &http.Server{
		Handler:           managedAuth(s.routes(), config),
		ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second,
		WriteTimeout: 75 * time.Second, IdleTimeout: 60 * time.Second,
	}
	go func() {
		io.Copy(io.Discard, reader)
		cancel()
		httpServer.Close()
	}()
	if err := json.NewEncoder(os.Stdout).Encode(map[string]any{
		"protocol_version": bridgeProtocolVersion, "endpoint": "http://" + listener.Addr().String(),
	}); err != nil {
		listener.Close()
		return fmt.Errorf("managed_handshake_failed")
	}
	err = httpServer.Serve(listener)
	cancel()
	s.upstream.close()
	if err == http.ErrServerClosed {
		return nil
	}
	return err
}

func managedAuth(next http.Handler, config managedConfig) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Public documents contain neither credentials nor session metadata.
		if r.Method == http.MethodGet && (r.URL.Path == "/login" || r.URL.Path == "/verify") {
			next.ServeHTTP(w, r)
			return
		}
		secret := r.Header.Get("Authorization")
		internal := subtle.ConstantTimeCompare([]byte(secret), []byte("Bearer "+config.Secret)) == 1
		login := config.LoginKey != "" && config.ExpiresAt > time.Now().Unix() &&
			subtle.ConstantTimeCompare([]byte(secret), []byte("Bearer "+config.LoginKey)) == 1
		loginRoute := r.URL.Path == "/v1/session/challenge" || r.URL.Path == "/v1/session/login"
		if !internal && !(login && loginRoute) {
			writeError(w, http.StatusUnauthorized, "bridge_auth_required")
			return
		}
		// Authorization-only workers must never connect to the message transport.
		if config.DirectoryOnly && !(r.Method == http.MethodGet &&
			(r.URL.Path == "/v1/status" || r.URL.Path == "/v1/groups" ||
				strings.HasPrefix(r.URL.Path, "/v1/groups/"))) {
			writeError(w, http.StatusConflict, "directory_only")
			return
		}
		if !config.Connect && (strings.HasPrefix(r.URL.Path, "/v1/media") || strings.HasPrefix(r.URL.Path, "/v1/actions") || strings.HasPrefix(r.URL.Path, "/v1/diagnostics") || r.URL.Path == "/v1/events" || r.URL.Path == "/v1/messages" ||
			(!config.DirectoryOnly && strings.HasPrefix(r.URL.Path, "/v1/groups"))) {
			writeError(w, http.StatusConflict, "authorization_only")
			return
		}
		next.ServeHTTP(w, r)
	})
}
