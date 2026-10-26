package main

import (
	_ "embed"
	"net"
	"net/http"
	"net/url"
)

//go:embed login.html
var loginPage []byte

//go:embed verify.html
var verificationPage []byte

func verificationOrigin(host string) string {
	u, err := url.Parse("http://" + host)
	if err != nil {
		return ""
	}
	other := "localhost"
	if u.Hostname() == "localhost" {
		other = "127.0.0.1"
	}
	if u.Port() != "" {
		other = net.JoinHostPort(other, u.Port())
	}
	return "http://" + other
}

func (s *server) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /login", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write(loginPage)
	})
	mux.HandleFunc("GET /verify", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Del("X-Frame-Options")
		w.Header().Set("Content-Security-Policy", "frame-ancestors "+verificationOrigin(r.Host))
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write(verificationPage)
	})
	mux.HandleFunc("GET /v1/session/challenge", s.challenge)
	mux.HandleFunc("GET /v1/status", s.status)
	mux.HandleFunc("POST /v1/session/login", s.login)
	mux.HandleFunc("POST /v1/session/logout", s.logout)
	mux.HandleFunc("POST /v1/session/refresh", s.refreshSession)
	mux.HandleFunc("GET /v1/groups", s.groups)
	mux.HandleFunc("GET /v1/groups/{id}/members", s.members)
	mux.HandleFunc("GET /v1/groups/member-availability", s.memberAvailability)
	mux.HandleFunc("POST /v1/messages", s.messages)
	mux.HandleFunc("POST /v1/media/images", s.imageUpload)
	mux.HandleFunc("POST /v1/actions", s.actions)
	mux.HandleFunc("GET /v1/actions/{id}", s.actionStatus)
	mux.HandleFunc("GET /v1/actions/group-state", s.groupState)
	mux.HandleFunc("GET /v1/diagnostics/messages", s.inspectMessages)
	mux.HandleFunc("GET /v1/events", s.stream)
	return localBrowserOnly(mux)
}

// Browsers must use the loopback page itself, not another site or local port.
// Origin-less calls remain available to the local AstrBot bridge client.
func localBrowserOnly(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("X-Frame-Options", "DENY")
		w.Header().Set("Referrer-Policy", "no-referrer")
		host, err := url.Parse("http://" + r.Host)
		if err != nil || host.User != nil || (host.Hostname() != "127.0.0.1" && host.Hostname() != "localhost" && host.Hostname() != "::1") {
			writeError(w, http.StatusForbidden, "loopback_host_required")
			return
		}
		// Only the public captcha document may be framed across loopback origins.
		if r.Method == http.MethodGet && r.URL.Path == "/verify" {
			next.ServeHTTP(w, r)
			return
		}
		if origin := r.Header.Get("Origin"); origin != "" {
			if origin != "http://"+r.Host {
				writeError(w, http.StatusForbidden, "same_origin_required")
				return
			}
		}
		if site := r.Header.Get("Sec-Fetch-Site"); site == "cross-site" || site == "same-site" {
			writeError(w, http.StatusForbidden, "same_origin_required")
			return
		}
		next.ServeHTTP(w, r)
	})
}
