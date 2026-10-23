package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestManagedAuthentication(t *testing.T) {
	handler := managedAuth(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(204)
	}), managedConfig{Secret: "internal", LoginKey: "browser", ExpiresAt: time.Now().Unix() + 900})
	for _, item := range []struct {
		path, secret string
		status       int
	}{
		{"/login", "", 204},
		{"/verify", "", 204},
		{"/v1/status", "", 401},
		{"/v1/status", "browser", 401},
		{"/v1/status", "internal", 204},
		{"/v1/session/challenge", "browser", 204},
		{"/v1/session/login", "browser", 204},
		{"/v1/events", "internal", 409},
		{"/v1/messages", "internal", 409},
		{"/v1/actions", "internal", 409},
		{"/v1/actions/group-state", "internal", 409},
		{"/v1/diagnostics/messages", "internal", 409},
		{"/v1/actions/operation-test", "browser", 401},
		{"/v1/groups/group/members", "internal", 409},
	} {
		r := httptest.NewRequest("GET", "http://127.0.0.1"+item.path, nil)
		if item.secret != "" {
			r.Header.Set("Authorization", "Bearer "+item.secret)
		}
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		if w.Code != item.status {
			t.Fatalf("%s: got %d, want %d", item.path, w.Code, item.status)
		}
	}
}

func TestExpiredLoginGrant(t *testing.T) {
	handler := managedAuth(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("expired authorization reached handler")
	}), managedConfig{Secret: "internal", LoginKey: "browser", ExpiresAt: time.Now().Unix() - 1})
	r := httptest.NewRequest("POST", "http://127.0.0.1/v1/session/login", nil)
	r.Header.Set("Authorization", "Bearer browser")
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, r)
	if w.Code != 401 {
		t.Fatalf("expired grant: %d", w.Code)
	}
}

func TestDirectoryOnlyWorker(t *testing.T) {
	handler := managedAuth(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(204)
	}), managedConfig{Secret: "internal", DirectoryOnly: true})
	for _, item := range []struct {
		method, path string
		status       int
	}{
		{"GET", "/v1/status", 204},
		{"GET", "/v1/groups", 204},
		{"GET", "/v1/groups/test/members", 204},
		{"GET", "/v1/events", 409},
		{"POST", "/v1/messages", 409},
		{"POST", "/v1/actions", 409},
		{"GET", "/v1/actions/group-state", 409},
		{"POST", "/v1/session/login", 409},
		{"POST", "/v1/session/logout", 409},
	} {
		r := httptest.NewRequest(item.method, "http://127.0.0.1"+item.path, nil)
		r.Header.Set("Authorization", "Bearer internal")
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		if w.Code != item.status {
			t.Fatalf("%s: got %d want %d", item.path, w.Code, item.status)
		}
	}
}
