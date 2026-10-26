package main

import (
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
)

func TestLocalRoutesProtectBrowserBoundary(t *testing.T) {
	s := &server{tokens: newTokenStore(filepath.Join(t.TempDir(), "token.json"))}
	if err := s.tokens.save(tokenFile{UserID: "mock-user", ChatToken: "private-chat-secret", IMToken: "private-im-secret"}); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name, method, path, host, origin, site string
		status                                 int
	}{
		{"page", "GET", "/login", "127.0.0.1:17890", "", "none", 200},
		{"local_adapter", "GET", "/v1/status", "127.0.0.1:17890", "", "", 200},
		{"forwarded_browser", "GET", "/v1/status", "127.0.0.1:17891", "http://127.0.0.1:17891", "same-origin", 200},
		{"external_origin", "POST", "/v1/session/logout", "127.0.0.1:17890", "https://untrusted.example", "", 403},
		{"other_local_port", "POST", "/v1/session/logout", "127.0.0.1:17890", "http://127.0.0.1:17891", "", 403},
		{"opaque_origin", "POST", "/v1/session/logout", "127.0.0.1:17890", "null", "", 403},
		{"cross_site_fetch", "POST", "/v1/session/logout", "127.0.0.1:17890", "", "cross-site", 403},
		{"dns_rebinding", "GET", "/v1/status", "untrusted.example:17890", "", "", 403},
		{"no_logout_get", "GET", "/v1/session/logout", "127.0.0.1:17890", "", "", 405},
		{"no_login_get", "GET", "/v1/session/login", "127.0.0.1:17890", "", "", 405},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := httptest.NewRequest(tc.method, "http://"+tc.host+tc.path, nil)
			r.Header.Set("Origin", tc.origin)
			r.Header.Set("Sec-Fetch-Site", tc.site)
			w := httptest.NewRecorder()
			s.routes().ServeHTTP(w, r)
			if w.Code != tc.status {
				t.Fatalf("status = %d, want %d", w.Code, tc.status)
			}
			if strings.Contains(w.Body.String(), "private-chat-secret") || strings.Contains(w.Body.String(), "private-im-secret") {
				t.Fatal("token exposed")
			}
			if w.Header().Get("Cache-Control") != "no-store" || w.Header().Get("X-Frame-Options") != "DENY" {
				t.Fatal("missing browser safety headers")
			}
		})
	}
	tokens, err := s.tokens.load()
	if err != nil || tokens.ChatToken != "private-chat-secret" {
		t.Fatal("rejected requests modified session")
	}
}

func TestChallengeReturnsOnlyPublicSceneSettings(t *testing.T) {
	for _, tc := range []struct {
		name, config string
		status       int
	}{
		{"enabled", `{"enabled":true,"provider":"aliyun","region":"cn","prefix":"mock-prefix","scenes":[{"usedFor":3,"enabled":true,"sceneId":"mock-scene"}],"unexpectedSecret":"must-not-return"}`, 200},
		{"disabled", `{"enabled":false}`, 200},
		{"unknown_provider", `{"enabled":true,"provider":"unknown","scenes":[{"usedFor":3,"enabled":true}]}`, 503},
		{"missing_scene", `{"enabled":true}`, 502},
	} {
		t.Run(tc.name, func(t *testing.T) {
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/client_config/get" || r.Header.Get("token") != "" {
					t.Error("unexpected account request")
				}
				w.Write([]byte(`{"errCode":0,"data":{"config":{"captcha":` + tc.config + `}}}`))
			}))
			defer upstream.Close()
			t.Setenv("LAJIAOLIAO_CHAT_ADDR", upstream.URL)
			w := httptest.NewRecorder()
			s := &server{}
			s.routes().ServeHTTP(w, httptest.NewRequest("GET", "http://127.0.0.1:17890/v1/session/challenge", nil))
			if w.Code != tc.status {
				t.Fatalf("status %d, want %d", w.Code, tc.status)
			}
			if strings.Contains(w.Body.String(), "must-not-return") {
				t.Fatal("raw upstream configuration leaked")
			}
			if tc.name == "enabled" && !strings.Contains(w.Body.String(), `"scene_id":"mock-scene"`) {
				t.Fatal("missing public scene identifier")
			}
		})
	}
}

func TestVerificationFrameOnlyAllowsOppositeLoopbackParent(t *testing.T) {
	s := &server{}
	r := httptest.NewRequest("GET", "http://localhost:17891/verify", nil)
	r.Header.Set("Sec-Fetch-Site", "cross-site")
	w := httptest.NewRecorder()
	s.routes().ServeHTTP(w, r)
	if w.Code != 200 || w.Header().Get("Content-Security-Policy") != "frame-ancestors http://127.0.0.1:17891" {
		t.Fatal("verification document must only allow opposite loopback parent")
	}
	if w.Header().Get("X-Frame-Options") != "" || strings.Contains(w.Body.String(), "id=\"password\"") {
		t.Fatal("verification frame must contain no credentials")
	}
}
