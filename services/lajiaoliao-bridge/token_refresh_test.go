package main

import (
	"bytes"
	"context"
	"encoding/gob"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/gorilla/websocket"
	"github.com/openimsdk/protocol/sdkws"
	"google.golang.org/protobuf/proto"
)

func TestConcurrentIMRefreshAndCredentialVersion(t *testing.T) {
	store := newTokenStore(filepath.Join(t.TempDir(), "token.json"))
	old := tokenFile{UserID: "bot", IMToken: "expired", ChatToken: "valid-chat", Version: 3}
	store.save(old)
	var calls atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("token") != "valid-chat" || r.URL.Path != "/pc/account/get_im_token" {
			t.Error("invalid token endpoint or header")
		}
		writeJSON(w, 200, map[string]any{"errCode": 0, "data": map[string]string{"imToken": "fresh"}})
	}))
	defer upstream.Close()
	t.Setenv("LAJIAOLIAO_CHAT_ADDR", upstream.URL)
	var wg sync.WaitGroup
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			got, err := store.refresh(context.Background(), old)
			if err != nil || got.IMToken != "fresh" || got.Version != 4 {
				t.Error("refresh failed")
			}
		}()
	}
	wg.Wait()
	if calls.Load() != 1 {
		t.Fatal("concurrent refresh was not coalesced")
	}
}

func TestRefreshRejectsBusinessTokenAndDifferentIdentity(t *testing.T) {
	for _, body := range []string{
		`{"errCode":1501}`, `{"errCode":1506}`,
		`{"errCode":0,"data":{"imToken":"fresh","userID":"another"}}`,
		`{"errCode":0,"data":{"imToken":"expired"}}`,
	} {
		t.Run(body, func(t *testing.T) {
			store := newTokenStore(filepath.Join(t.TempDir(), "token.json"))
			old := tokenFile{UserID: "bot", IMToken: "expired", ChatToken: "valid-chat"}
			store.save(old)
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Write([]byte(body))
			}))
			defer upstream.Close()
			t.Setenv("LAJIAOLIAO_CHAT_ADDR", upstream.URL)
			if _, err := store.refresh(context.Background(), old); err == nil {
				t.Fatal("invalid renewal succeeded")
			}
			got, _ := store.load()
			if got != old {
				t.Fatal("failed renewal overwrote the old token")
			}
		})
	}
}

func TestOnlyExplicitExpiryRenewsAndNeverRetriesMessage(t *testing.T) {
	for _, code := range []int{1501, 1502, 1506, 1002} {
		t.Run(fmt.Sprint(code), func(t *testing.T) {
			store := newTokenStore(filepath.Join(t.TempDir(), "token.json"))
			store.save(tokenFile{UserID: "bot", IMToken: "expired", ChatToken: "chat"})
			var refreshes atomic.Int32
			chat := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				refreshes.Add(1)
				w.Write([]byte(`{"errCode":0,"data":{"imToken":"fresh"}}`))
			}))
			defer chat.Close()
			t.Setenv("LAJIAOLIAO_CHAT_ADDR", chat.URL)
			ws := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				c, err := (&websocket.Upgrader{}).Upgrade(w, r, nil)
				if err != nil {
					return
				}
				defer c.Close()
				_, raw, _ := c.ReadMessage()
				var req wireRequest
				gob.NewDecoder(bytes.NewReader(raw)).Decode(&req)
				response := wireResponse{ReqIdentifier: req.ReqIdentifier, MsgIncr: req.MsgIncr, ErrCode: code}
				if r.URL.Query().Get("token") == "fresh" {
					response.ErrCode = 0
					response.Data, _ = proto.Marshal(&sdkws.GetMaxSeqResp{})
				}
				var buffer bytes.Buffer
				gob.NewEncoder(&buffer).Encode(response)
				c.WriteMessage(websocket.BinaryMessage, buffer.Bytes())
			}))
			defer ws.Close()
			t.Setenv("LAJIAOLIAO_WS_ADDR", "ws"+strings.TrimPrefix(ws.URL, "http"))
			o := &openIM{store: store}
			defer o.close()
			o.sequences(context.Background())
			if code == 1501 {
				if refreshes.Load() != 1 {
					t.Fatal("expired token did not renew")
				}
				if _, err := o.sequences(context.Background()); err != nil {
					t.Fatal("fresh token did not reconnect")
				}
			} else if refreshes.Load() != 0 {
				t.Fatal("non-expiry error renewed a token")
			}
		})
	}
}

func TestReauthorizationRejectsDifferentUserBeforeSaving(t *testing.T) {
	chat := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/client_config/get":
			w.Write([]byte(`{"errCode":0,"data":{"config":{"captcha":{"enabled":false}}}}`))
		case "/pc/account/login":
			w.Write([]byte(`{"errCode":0,"data":{"userID":"other","imToken":"other-im","chatToken":"other-chat"}}`))
		default:
			t.Error("a different user must not obtain the web token")
			w.WriteHeader(500)
		}
	}))
	defer chat.Close()
	t.Setenv("LAJIAOLIAO_CHAT_ADDR", chat.URL)
	store := newTokenStore(filepath.Join(t.TempDir(), "token.json"))
	s := &server{tokens: store}
	request := httptest.NewRequest("POST", "/v1/session/login", strings.NewReader(
		`{"account":"13800000000","password":"synthetic","expected_user_id":"original"}`))
	w := httptest.NewRecorder()
	s.login(w, request)
	if w.Code != 409 {
		t.Fatal("different-account login was not rejected")
	}
	value, err := store.load()
	if err != nil || value.UserID != "" {
		t.Fatal("different-account credentials persisted")
	}
}

func TestRefreshVersionConflictDoesNotOverwriteNewLogin(t *testing.T) {
	store := newTokenStore(filepath.Join(t.TempDir(), "token.json"))
	old := tokenFile{UserID: "bot", IMToken: "expired", ChatToken: "old-chat"}
	freshLogin := tokenFile{UserID: "bot", IMToken: "new-login", ChatToken: "new-chat", Version: 5}
	store.save(old)
	chat := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		store.save(freshLogin)
		json.NewEncoder(w).Encode(map[string]any{"errCode": 0, "data": map[string]string{"imToken": "renewed-old"}})
	}))
	defer chat.Close()
	t.Setenv("LAJIAOLIAO_CHAT_ADDR", chat.URL)
	if _, err := store.refresh(context.Background(), old); err == nil {
		t.Fatal("stale refresh committed")
	}
	current, _ := store.load()
	if current != freshLogin {
		t.Fatal("new login overwritten")
	}
}
