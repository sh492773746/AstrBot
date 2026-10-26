package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/gorilla/websocket"
)

func TestUpstreamCredentialsNeverFollowRedirect(t *testing.T) {
	var forwarded int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&forwarded, 1)
		w.Write([]byte(`{"errCode":0}`))
	}))
	defer target.Close()
	for _, status := range []int{301, 302, 303, 307, 308} {
		source := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Location", target.URL)
			w.WriteHeader(status)
		}))
		_, err := postJSON(context.Background(), source.URL, map[string]string{"passwordMD5": "synthetic"}, "synthetic-token")
		source.Close()
		if err == nil || !strings.Contains(err.Error(), "upstream status") {
			t.Fatalf("redirect must be rejected: status %d", status)
		}
	}
	if atomic.LoadInt32(&forwarded) != 0 {
		t.Fatal("credentials forwarded to redirect target")
	}
}

func TestOversizedUpstreamResponseIsRejected(t *testing.T) {
	source := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"errCode":0}` + strings.Repeat(" ", 2<<20)))
	}))
	defer source.Close()
	_, err := postJSON(context.Background(), source.URL, map[string]string{}, "")
	if err == nil || !strings.Contains(err.Error(), "too large") {
		t.Fatal("oversized response was not rejected")
	}
}

func TestLoginRejectsMissingCredentialsWithoutPersistingToken(t *testing.T) {
	store := newTokenStore(filepath.Join(t.TempDir(), "token.json"))
	server := &server{
		broker: &broker{peers: make(map[*websocket.Conn]struct{})},
		tokens: store,
	}

	request := httptest.NewRequest(http.MethodPost, "/v1/session/login", nil)
	response := httptest.NewRecorder()
	server.login(response, request)

	if response.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusBadRequest)
	}
	if _, err := store.load(); err != nil {
		t.Fatalf("token store should remain readable: %v", err)
	}
}

func TestExtractTokensSupportsNestedLoginResponse(t *testing.T) {
	data, err := json.Marshal(map[string]any{
		"errCode": 0,
		"data": map[string]string{
			"userID":    "user-1",
			"imToken":   "im-token",
			"chatToken": "chat-token",
		},
	})
	if err != nil {
		t.Fatal(err)
	}

	tokens, ok := extractTokens(data)
	if !ok {
		t.Fatal("expected token response to be accepted")
	}
	if tokens.UserID != "user-1" || tokens.IMToken != "im-token" || tokens.ChatToken != "chat-token" {
		t.Fatalf("unexpected tokens: %#v", tokens)
	}
}

func TestLoginRequiresHumanCaptchaBeforeSendingCredentials(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/client_config/get" {
			t.Errorf("credentials must not be sent to %s", r.URL.Path)
			w.WriteHeader(http.StatusForbidden)
			return
		}
		writeJSON(w, 200, map[string]any{"errCode": 0, "data": map[string]any{"config": map[string]any{"captcha": map[string]any{
			"enabled": true, "scenes": []map[string]any{{"usedFor": 3, "enabled": true}},
		}}}})
	}))
	defer upstream.Close()
	t.Setenv("LAJIAOLIAO_CHAT_ADDR", upstream.URL)
	store := newTokenStore(filepath.Join(t.TempDir(), "token.json"))
	server := &server{
		broker: &broker{peers: make(map[*websocket.Conn]struct{})},
		tokens: store,
	}

	request := httptest.NewRequest(
		http.MethodPost,
		"/v1/session/login",
		strings.NewReader(`{"account":"13800000000","password":"not-persisted"}`),
	)
	response := httptest.NewRecorder()
	server.login(response, request)

	if response.Code != http.StatusPreconditionRequired {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusPreconditionRequired)
	}
	var payload errorResponse
	if err := json.NewDecoder(response.Body).Decode(&payload); err != nil {
		t.Fatal(err)
	}
	if payload.Error != "lajiaoliao_captcha_required" {
		t.Fatalf("error = %q, want lajiaoliao_captcha_required", payload.Error)
	}
	if _, err := os.Stat(store.path); !os.IsNotExist(err) {
		t.Fatal("blocked login must not persist credentials")
	}
}

func TestAccountRoutesUseChatHostWithoutBootstrapToken(t *testing.T) {
	for _, tc := range []struct{ name, proof string }{
		{"captcha_disabled", ""},
		{"user_completed_captcha", "mock-user-completed-proof"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			im := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				t.Error("account requests reached OpenIM host")
				w.WriteHeader(500)
			}))
			defer im.Close()
			t.Setenv("LAJIAOLIAO_API_ADDR", im.URL)
			var paths []string
			chat := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				paths = append(paths, r.URL.Path)
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				switch r.URL.Path {
				case "/client_config/get":
					writeJSON(w, 200, map[string]any{"errCode": 0, "data": map[string]any{"config": map[string]any{"captcha": map[string]any{"enabled": tc.proof != "", "scenes": []map[string]any{{"usedFor": 3, "enabled": true}}}}}})
				case "/pc/account/login":
					if r.Header.Get("token") != "" {
						t.Error("fresh login must not require a token")
					}
					if r.Header.Get("platform") != "3" || body["platform"] != float64(3) || body["deviceID"] != r.Header.Get("deviceID") {
						t.Error("login metadata mismatch")
					}
					if body["password"] != nil || len(body["passwordMD5"].(string)) != 32 {
						t.Error("expected hashed password only")
					}
					if body["confirmSameClassDeviceSwitch"] != false {
						t.Error("must not confirm device switch automatically")
					}
					if tc.proof != "" && body["captchaVerifyParam"] != tc.proof {
						t.Error("missing user challenge proof")
					}
					writeJSON(w, 200, map[string]any{"errCode": 0, "data": map[string]string{"userID": "test-user", "chatToken": "mock-chat", "imToken": "mock-old-im"}})
				case "/pc/account/get_im_token":
					if r.Header.Get("token") != "mock-chat" || body["platform"] != float64(5) {
						t.Error("refresh must use issued chat token and web platform")
					}
					writeJSON(w, 200, map[string]any{"errCode": 0, "data": map[string]string{"imToken": "mock-new-im"}})
				default:
					t.Error("unexpected path")
					w.WriteHeader(404)
				}
			}))
			defer chat.Close()
			t.Setenv("LAJIAOLIAO_CHAT_ADDR", chat.URL)
			store := newTokenStore(filepath.Join(t.TempDir(), "session", "token.json"))
			s := &server{tokens: store}
			request, _ := json.Marshal(loginRequest{Account: "13800000000", Password: "mock-password", CaptchaVerifyParam: tc.proof})
			w := httptest.NewRecorder()
			s.login(w, httptest.NewRequest("POST", "/v1/session/login", strings.NewReader(string(request))))
			if w.Code != 200 {
				t.Fatalf("status %d", w.Code)
			}
			if len(paths) != 3 {
				t.Fatalf("request count %d", len(paths))
			}
			tokens, err := store.load()
			if err != nil || tokens.ChatToken != "mock-chat" || tokens.IMToken != "mock-new-im" {
				t.Fatal("issued tokens not saved correctly")
			}
			info, err := os.Stat(store.path)
			if err != nil || info.Mode().Perm() != 0600 {
				t.Fatal("token file must be private")
			}
			raw, _ := os.ReadFile(store.path)
			if strings.Contains(string(raw), "mock-password") || strings.Contains(w.Body.String(), "mock-chat") {
				t.Fatal("secret leaked into store or response")
			}
		})
	}
}

func TestLoginFailuresDoNotReplaceExistingSession(t *testing.T) {
	for _, tc := range []struct {
		name, stage, response string
		status                int
	}{
		{"config_error", "config", `{"errCode":1001}`, 502},
		{"config_missing", "config", `{"errCode":0,"data":{}}`, 502},
		{"login_rejected", "login", `{"errCode":1001,"data":{"userID":"x","chatToken":"x","imToken":"x"}}`, 502},
		{"device_confirmation", "login", `{"errCode":0,"data":{"needConfirmSameClassDeviceSwitch":true}}`, 409},
		{"refresh_rejected", "refresh", `{"errCode":1001}`, 502},
		{"refresh_missing_token", "refresh", `{"errCode":0,"data":{}}`, 502},
	} {
		t.Run(tc.name, func(t *testing.T) {
			chat := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				stage := map[string]string{"/client_config/get": "config", "/pc/account/login": "login", "/pc/account/get_im_token": "refresh"}[r.URL.Path]
				if stage == tc.stage {
					w.Write([]byte(tc.response))
					return
				}
				if stage == "config" {
					w.Write([]byte(`{"errCode":0,"data":{"config":{"captcha":{"enabled":false}}}}`))
					return
				}
				w.Write([]byte(`{"errCode":0,"data":{"userID":"new","chatToken":"new-chat","imToken":"new-im"}}`))
			}))
			defer chat.Close()
			t.Setenv("LAJIAOLIAO_CHAT_ADDR", chat.URL)
			store := newTokenStore(filepath.Join(t.TempDir(), "token.json"))
			original := tokenFile{UserID: "existing", ChatToken: "existing-chat", IMToken: "existing-im"}
			if err := store.save(original); err != nil {
				t.Fatal(err)
			}
			w := httptest.NewRecorder()
			s := &server{tokens: store}
			s.login(w, httptest.NewRequest("POST", "/v1/session/login", strings.NewReader(`{"account":"13800000000","password":"mock-password"}`)))
			if w.Code != tc.status {
				t.Fatalf("status %d, want %d", w.Code, tc.status)
			}
			saved, err := store.load()
			if err != nil || saved != original {
				t.Fatal("failed login replaced existing session")
			}
		})
	}
}
