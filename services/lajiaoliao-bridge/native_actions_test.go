package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"sync/atomic"
	"testing"
)

func TestNativeFixedActionsAndProtection(t *testing.T) {
	for _, action := range []string{"mute", "unmute", "kick", "announce", "mute_all", "unmute_all"} {
		for _, mode := range []string{"success", "unknown", "ordinary", "protected", "conflict"} {
			t.Run(action+"/"+mode, func(t *testing.T) {
				var mutations atomic.Int32
				upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					var body map[string]any
					if json.NewDecoder(r.Body).Decode(&body) != nil || r.Header.Get("token") != "im" {
						t.Error("invalid authenticated request")
					}
					if r.URL.Path == "/group/get_group_members_info" {
						uid := body["userIDs"].([]any)[0].(string)
						role := 20
						if uid == "bot" {
							role = 60
							if mode == "ordinary" {
								role = 20
							}
						} else if mode == "protected" {
							role = 60
						}
						writeJSON(w, 200, map[string]any{"errCode": 0, "data": map[string]any{
							"members": []map[string]any{{"groupID": "g", "userID": uid, "roleLevel": role}},
						}})
						return
					}
					if r.URL.Path == "/group/get_groups_info" {
						text := "old"
						if mode == "conflict" {
							text = "changed"
						}
						writeJSON(w, 200, map[string]any{"errCode": 0, "data": map[string]any{
							"groupInfos": []map[string]string{{"groupID": "g", "notification": text}},
						}})
						return
					}
					routes := map[string]string{
						"mute": "/group/mute_group_member", "unmute": "/group/cancel_mute_group_member",
						"kick": "/group/kick_group", "announce": "/group/set_group_info",
						"mute_all": "/group/mute_group", "unmute_all": "/group/cancel_mute_group",
					}
					if r.URL.Path != routes[action] {
						t.Error("escaped fixed route")
					}
					if action == "announce" {
						info := body["groupInfoForSet"].(map[string]any)
						if len(body) != 1 || len(info) != 2 || info["notification"] != "new" || info["groupID"] != "g" {
							t.Error("announcement escaped fixed fields")
						}
					} else if body["groupID"] != "g" {
						t.Error("escaped group")
					}
					if action == "mute" && body["mutedSeconds"] != float64(600) {
						t.Error("wrong duration")
					}
					if action == "kick" && body["kickedUserIDs"].([]any)[0] != "person" {
						t.Error("wrong kick target")
					}
					mutations.Add(1)
					if mode == "unknown" {
						w.WriteHeader(502)
					} else {
						writeJSON(w, 200, map[string]int{"errCode": 0})
					}
				}))
				defer upstream.Close()
				t.Setenv("LAJIAOLIAO_API_ADDR", upstream.URL)
				tokens := newTokenStore(filepath.Join(t.TempDir(), "token.json"))
				if err := tokens.save(tokenFile{UserID: "bot", IMToken: "im"}); err != nil {
					t.Fatal(err)
				}
				s := &server{tokens: tokens, upstream: &openIM{state: "online", user: "bot"}}
				input := actionRequest{OperationID: "operation-test", Action: action, Kind: "group", TargetID: "g"}
				if action == "mute" || action == "unmute" || action == "kick" {
					input.UserID = "person"
				}
				if action == "mute" {
					input.MutedSeconds = 600
				}
				if action == "kick" {
					input.ConfirmKick = true
				}
				old := "old"
				if action == "announce" {
					input.Announcement, input.ExpectedAnnouncement = "new", &old
				}
				submit := func() *httptest.ResponseRecorder {
					raw, _ := json.Marshal(input)
					w := httptest.NewRecorder()
					s.actions(w, httptest.NewRequest("POST", "/v1/actions", bytes.NewReader(raw)))
					return w
				}
				result := submit()
				denied := mode == "ordinary" || (mode == "protected" && input.UserID != "") || (mode == "conflict" && action == "announce")
				if denied {
					if result.Code != 403 || mutations.Load() != 0 {
						t.Fatalf("unauthorized mutation: %s", result.Body.String())
					}
					return
				}
				var receipt actionReceipt
				json.Unmarshal(result.Body.Bytes(), &receipt)
				expected := "succeeded"
				if mode == "unknown" {
					expected = "unknown"
				}
				if result.Code != 200 || receipt.Status != expected || mutations.Load() != 1 {
					t.Fatalf("incorrect receipt: %s", result.Body.String())
				}
				s = &server{tokens: tokens, upstream: &openIM{}}
				if submit().Body.String() != result.Body.String() || mutations.Load() != 1 {
					t.Fatal("restart repeated effect")
				}
			})
		}
	}
}

func TestNativeValidationNeverAcceptsArbitraryEffects(t *testing.T) {
	valid := actionRequest{Action: "mute", Kind: "group", TargetID: "g", UserID: "u", MutedSeconds: 60}
	if validateNativeAction(valid) != nil {
		t.Fatal("valid mute rejected")
	}
	for _, input := range []actionRequest{
		{Action: "mute", Kind: "private", UserID: "u", MutedSeconds: 60},
		{Action: "mute", Kind: "group", UserID: "u", MutedSeconds: 86460},
		{Action: "kick", Kind: "group", UserID: "u"},
		{Action: "announce", Kind: "group", Announcement: "unsafe"},
		{Action: "mute_all", Kind: "group", UserID: "u"},
		{Action: "mute_all", Kind: "group", MessageID: "arbitrary"},
	} {
		if validateNativeAction(input) == nil {
			t.Fatal("unsafe action accepted")
		}
	}
}
