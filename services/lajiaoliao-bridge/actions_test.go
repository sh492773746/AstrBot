package main

import (
	"bytes"
	"encoding/gob"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/gorilla/websocket"
	"github.com/openimsdk/protocol/sdkws"
	"google.golang.org/protobuf/proto"
)

func TestSelfActionsAndDurableReceipts(t *testing.T) {
	for _, tc := range []struct {
		name, action, nickname, sender, result string
		code                                   int
	}{
		{"nickname", "set_self_group_nickname", "old", "bot", "succeeded", 200},
		{"nickname stale", "set_self_group_nickname", "changed", "bot", "", 409},
		{"revoke own", "revoke_self_message", "", "bot", "succeeded", 200},
		{"revoke foreign", "revoke_self_message", "", "user", "rejected", 403},
		{"lost nickname receipt", "set_self_group_nickname", "old", "bot", "unknown", 200},
		{"lost revoke receipt", "revoke_self_message", "", "bot", "unknown", 200},
		{"native permission denial", "set_self_group_nickname", "old", "bot", "rejected", 200},
		{"member nickname", "set_member_group_nickname", "old", "user", "succeeded", 200},
		{"member nickname stale", "set_member_group_nickname", "changed", "user", "", 409},
		{"member nickname protected", "set_member_group_nickname", "old", "user", "rejected", 403},
		{"member actor missing role", "set_member_group_nickname", "old", "user", "rejected", 403},
		{"member lookup cross group", "set_member_group_nickname", "old", "user", "rejected", 403},
		{"revoke member", "revoke_member_message", "", "user", "succeeded", 200},
		{"revoke other identity", "revoke_member_message", "", "other", "rejected", 403},
		{"lost member revoke receipt", "revoke_member_message", "", "user", "unknown", 200},
		{"lost member nickname receipt", "set_member_group_nickname", "old", "user", "unknown", 200},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store := newTokenStore(filepath.Join(t.TempDir(), "token.json"))
			if store.save(tokenFile{UserID: "bot", IMToken: "im", ChatToken: "chat"}) != nil {
				t.Fatal("token save failed")
			}
			var mutations atomic.Int32
			httpUpstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("token") != "im" {
					t.Error("incorrect upstream authentication")
				}
				var body map[string]any
				json.NewDecoder(r.Body).Decode(&body)
				if r.URL.Path == "/group/get_group_members_info" {
					uid := body["userIDs"].([]any)[0].(string)
					memberAction := tc.action == "set_member_group_nickname" || tc.action == "revoke_member_message"
					if body["groupID"] != "g1" || (uid != "bot" && (!memberAction || uid != "user")) {
						t.Error("membership lookup escaped account")
					}
					role := int32(100)
					group := "g1"
					if uid == "user" {
						role = 20
						if tc.name == "member nickname protected" {
							role = 60
						}
						if tc.name == "member lookup cross group" {
							group = "g2"
						}
					} else if tc.name == "member actor missing role" {
						role = 20
					}
					writeJSON(w, 200, map[string]any{"errCode": 0, "data": map[string]any{
						"members": []groupMember{{GroupID: group, UserID: uid, Nickname: tc.nickname, RoleLevel: role}},
					}})
					return
				}
				mutations.Add(1)
				if tc.action == "revoke_self_message" || tc.action == "revoke_member_message" {
					if r.URL.Path != "/msg/revoke_msg" || body["userID"] != "bot" ||
						body["conversationID"] != "sg_g1" || body["seq"] != float64(1) {
						t.Error("incorrect native recall payload")
					}
				} else {
					member := body["members"].([]any)[0].(map[string]any)
					expected := "bot"
					if tc.action == "set_member_group_nickname" {
						expected = "user"
					}
					if r.URL.Path != "/group/set_group_member_info" || len(member) != 3 ||
						member["userID"] != expected || member["nickname"] != "new" || member["groupID"] != "g1" {
						t.Error("nickname mutation escaped fixed fields")
					}
				}
				if tc.result == "unknown" {
					w.WriteHeader(502)
				} else if tc.result == "rejected" {
					writeJSON(w, 200, map[string]int{"errCode": 1003})
				} else {
					writeJSON(w, 200, map[string]any{"errCode": 0, "data": map[string]any{}})
				}
			}))
			defer httpUpstream.Close()
			t.Setenv("LAJIAOLIAO_API_ADDR", httpUpstream.URL)
			socket := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				conn, err := (&websocket.Upgrader{}).Upgrade(w, r, nil)
				if err != nil {
					return
				}
				defer conn.Close()
				for {
					_, raw, err := conn.ReadMessage()
					if err != nil {
						return
					}
					var req wireRequest
					gob.NewDecoder(bytes.NewReader(raw)).Decode(&req)
					var out proto.Message
					if req.ReqIdentifier == 1001 {
						out = &sdkws.GetMaxSeqResp{MaxSeqs: map[string]int64{"sg_g1": 1}}
					} else if req.ReqIdentifier == 1002 {
						out = &sdkws.PullMessageBySeqsResp{Msgs: map[string]*sdkws.PullMsgs{
							"sg_g1": {Msgs: []*sdkws.MsgData{{
								ClientMsgID: "own-message", SendID: tc.sender, RecvID: "g1", GroupID: "g1",
								SessionType: 3, Seq: 1, ContentType: 101, Content: []byte(`{"content":"hello"}`),
							}}},
						}}
					} else {
						t.Error("mutation unexpectedly used message send RPC")
						return
					}
					payload, _ := proto.Marshal(out)
					var buffer bytes.Buffer
					gob.NewEncoder(&buffer).Encode(wireResponse{ReqIdentifier: req.ReqIdentifier, MsgIncr: req.MsgIncr, Data: payload})
					conn.WriteMessage(websocket.BinaryMessage, buffer.Bytes())
				}
			}))
			defer socket.Close()
			t.Setenv("LAJIAOLIAO_WS_ADDR", "ws"+strings.TrimPrefix(socket.URL, "http"))
			o := &openIM{store: store, state: "online", user: "bot"}
			defer o.close()
			s := &server{tokens: store, upstream: o}
			old := "old"
			input := actionRequest{OperationID: "operation-test", Action: tc.action, Kind: "group", TargetID: "g1"}
			if tc.action == "set_self_group_nickname" || tc.action == "set_member_group_nickname" {
				input.Nickname, input.ExpectedNickname = "new", &old
			} else {
				input.MessageID = "own-message"
			}
			if tc.action == "revoke_member_message" || tc.action == "set_member_group_nickname" {
				input.UserID = "user"
			}
			raw, _ := json.Marshal(input)
			submit := func(server *server) *httptest.ResponseRecorder {
				w := httptest.NewRecorder()
				server.actions(w, httptest.NewRequest("POST", "/v1/actions", bytes.NewReader(raw)))
				return w
			}
			first := submit(s)
			if first.Code != tc.code {
				t.Fatalf("status %d: %s", first.Code, first.Body.String())
			}
			var result actionReceipt
			json.Unmarshal(first.Body.Bytes(), &result)
			if result.Status != tc.result {
				t.Fatalf("unexpected receipt: %s", first.Body.String())
			}
			if (tc.code == 200 && mutations.Load() != 1) || (tc.code != 200 && mutations.Load() != 0) {
				t.Fatal("unexpected upstream mutation count")
			}
			if mutations.Load() > 0 {
				// Process restart retains idempotency, even when offline.
				second := submit(&server{tokens: store, upstream: &openIM{}})
				if second.Body.String() != first.Body.String() || mutations.Load() != 1 {
					t.Fatal("duplicate operation repeated or receipt changed")
				}
				query := httptest.NewRequest("GET", "/v1/actions/operation-test", nil)
				query.SetPathValue("id", "operation-test")
				status := httptest.NewRecorder()
				s.actionStatus(status, query)
				if status.Body.String() != first.Body.String() || mutations.Load() != 1 {
					t.Fatal("receipt query changed or repeated operation")
				}
				info, _ := os.Stat(filepath.Join(filepath.Dir(store.path), "actions.json"))
				if info.Mode().Perm() != 0600 {
					t.Fatal("action ledger must be private")
				}
				input.Nickname = "different"
				raw, _ = json.Marshal(input)
				if input.Action == "set_self_group_nickname" && submit(s).Code != 409 {
					t.Fatal("operation ID reused for different payload")
				}
			}
		})
	}
}

func TestActionsRejectArbitraryMemberFields(t *testing.T) {
	s := &server{}
	for _, body := range []string{
		`{"operation_id":"operation-test","action":"set_self_group_nickname","kind":"group","target_id":"g1","nickname":"ok","expected_nickname":"old","user_id":"other"}`,
		`{"operation_id":"operation-test","action":"set_self_group_nickname","kind":"group","target_id":"g1","nickname":"bad\nname","expected_nickname":"old"}`,
		`{"operation_id":"operation-test","action":"set_self_group_nickname","kind":"private","target_id":"g1","nickname":"ok","expected_nickname":"old"}`,
		`{"operation_id":"operation-test","action":"revoke_self_message","kind":"group","target_id":"g1","message_id":"own","roleLevel":100}`,
	} {
		w := httptest.NewRecorder()
		s.actions(w, httptest.NewRequest("POST", "/v1/actions", strings.NewReader(body)))
		if w.Code != 400 {
			t.Fatal("arbitrary action accepted")
		}
	}
}
