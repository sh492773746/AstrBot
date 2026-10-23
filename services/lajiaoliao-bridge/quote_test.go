package main

import (
	"bytes"
	"encoding/gob"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/gorilla/websocket"
	msgproto "github.com/openimsdk/protocol/msg"
	"github.com/openimsdk/protocol/sdkws"
	"google.golang.org/protobuf/proto"
)

func TestQuoteIncomingAndConversationValidation(t *testing.T) {
	original := &sdkws.MsgData{
		ClientMsgID: "original", ServerMsgID: "original-server", SendID: "bot",
		RecvID: "g1", GroupID: "g1", SessionType: 3, ContentType: 101,
		Content: []byte(`{"content":"original text"}`), SendTime: 12000,
		SenderNickname: "Bot",
	}
	content, err := quoteEnvelope("new reply", original)
	if err != nil {
		t.Fatal(err)
	}
	message := &sdkws.MsgData{
		ClientMsgID: "reply", SendID: "user", RecvID: "g1", GroupID: "g1",
		SessionType: 3, ContentType: 114, Content: content, SendTime: 14000,
	}
	event := textMessage(message, "bot")
	if event["text"] != "new reply" || event["target_id"] != "g1" {
		t.Fatal("quote body must be the new text, not the quoted text")
	}
	q := event["components"].([]map[string]any)[0]
	if q["message_id"] != "original" || q["text"] != "original text" ||
		q["sender_id"] != "bot" || q["timestamp"] != int64(12) {
		t.Fatal("quote metadata lost")
	}
	if textMessage(message, "user") != nil {
		t.Fatal("self quote was not ignored")
	}
	message.GroupID = "another-group"
	if textMessage(message, "bot") != nil {
		t.Fatal("cross-group quote accepted")
	}
	message.GroupID, message.SessionType, message.RecvID = "", 1, "bot"
	if textMessage(message, "bot") != nil {
		t.Fatal("group quote accepted as private")
	}
	original.GroupID, original.SessionType, original.RecvID = "", 1, "user"
	message.Content, _ = quoteEnvelope("private reply", original)
	if textMessage(message, "bot") == nil {
		t.Fatal("valid private quote rejected")
	}
	original.RecvID = "other-user"
	message.Content, _ = quoteEnvelope("private reply", original)
	if textMessage(message, "bot") != nil {
		t.Fatal("cross-private quote accepted")
	}
	for _, body := range []string{`{`, `{"text":"x"}`, `{"text":"x","quoteMessage":{}}`} {
		message.Content = []byte(body)
		if textMessage(message, "bot") != nil {
			t.Fatal("malformed quote became a valid event")
		}
	}
}

func TestQuoteWireLookupAndReceipt(t *testing.T) {
	for _, tc := range []struct {
		name       string
		kind       string
		component  bool
		nested     bool
		foreign    bool
		missing    bool
		media      bool
		unknown    bool
		wantStatus int
	}{
		{name: "group reply_to", kind: "group", wantStatus: 200},
		{name: "group component", kind: "group", component: true, wantStatus: 200},
		{name: "private", kind: "private", wantStatus: 200},
		{name: "flatten nested quote", kind: "group", nested: true, wantStatus: 200},
		{name: "cross conversation", kind: "group", foreign: true, wantStatus: 422},
		{name: "older than 200 messages", kind: "group", missing: true, wantStatus: 422},
		{name: "media quote", kind: "group", media: true, wantStatus: 422},
		{name: "unknown send", kind: "group", unknown: true, wantStatus: 502},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store := newTokenStore(filepath.Join(t.TempDir(), "token.json"))
			if err := store.save(tokenFile{UserID: "bot", IMToken: "mock-im", ChatToken: "mock-chat"}); err != nil {
				t.Fatal(err)
			}
			conversation, target := "sg_group", "group"
			original := &sdkws.MsgData{
				SendID: "user", RecvID: target, GroupID: target, SessionType: 3,
				ClientMsgID: "original", ServerMsgID: "server-original",
				ContentType: 101, Content: []byte(`{"content":"original text"}`),
				Seq: 1000, SendTime: 12000, SenderNickname: "User",
			}
			if tc.kind == "private" {
				conversation, target = "si_bot_user", "user"
				original.GroupID, original.RecvID, original.SessionType = "", "bot", 1
			}
			if tc.foreign {
				original.GroupID = "other-group"
			}
			if tc.media {
				original.ContentType = 102
			}
			if tc.nested {
				base := &sdkws.MsgData{
					ClientMsgID: "nested-base", SendID: "bot", RecvID: target,
					GroupID: target, SessionType: 3, ContentType: 101,
					Content: []byte(`{"content":"base text"}`),
				}
				original.Content, _ = quoteEnvelope("original text", base)
				original.ContentType = 114
			}
			var sends, pulls atomic.Int32
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
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
					if gob.NewDecoder(bytes.NewReader(raw)).Decode(&req) != nil {
						t.Error("invalid gob request")
						return
					}
					var out proto.Message
					switch req.ReqIdentifier {
					case 1001:
						out = &sdkws.GetMaxSeqResp{MaxSeqs: map[string]int64{conversation: 1000}}
					case 1002:
						pulls.Add(1)
						var pull sdkws.PullMessageBySeqsReq
						if proto.Unmarshal(req.Data, &pull) != nil || pull.UserID != "bot" ||
							len(pull.SeqRanges) != 1 || pull.SeqRanges[0].ConversationID != conversation ||
							pull.SeqRanges[0].Begin < 801 || pull.SeqRanges[0].End > 1000 {
							t.Error("unbounded or foreign conversation lookup")
						}
						out = &sdkws.PullMessageBySeqsResp{}
						if !tc.missing {
							out = &sdkws.PullMessageBySeqsResp{Msgs: map[string]*sdkws.PullMsgs{
								conversation: {Msgs: []*sdkws.MsgData{original}},
							}}
						}
					case 1003:
						sends.Add(1)
						var msg sdkws.MsgData
						if proto.Unmarshal(req.Data, &msg) != nil {
							t.Error("invalid wire message")
						}
						var body quoteContent
						if json.Unmarshal(msg.Content, &body) != nil || body.Text != "new reply" ||
							body.QuoteMessage == nil || body.QuoteMessage.ClientMsgID != "original" ||
							body.QuoteMessage.TextElem == nil || body.QuoteMessage.TextElem.Content != "original text" ||
							body.QuoteMessage.ContentType != 101 || msg.ContentType != 114 ||
							msg.SendID != "bot" || msg.RecvID != target {
							t.Error("incorrect SDK quote envelope")
						}
						if tc.unknown {
							return // A lost receipt must not trigger another send.
						}
						out = &msgproto.SendMsgResp{ClientMsgID: msg.ClientMsgID, ServerMsgID: "quote-server", SendTime: 14000}
					default:
						t.Error("unexpected RPC")
						return
					}
					payload, _ := proto.Marshal(out)
					var buffer bytes.Buffer
					gob.NewEncoder(&buffer).Encode(wireResponse{ReqIdentifier: req.ReqIdentifier, MsgIncr: req.MsgIncr, Data: payload})
					conn.WriteMessage(websocket.BinaryMessage, buffer.Bytes())
				}
			}))
			defer upstream.Close()
			t.Setenv("LAJIAOLIAO_WS_ADDR", "ws"+strings.TrimPrefix(upstream.URL, "http"))
			o := &openIM{store: store}
			defer o.close()
			s := &server{tokens: store, upstream: o}
			body := map[string]any{
				"kind": tc.kind, "target_id": target, "reply_to": "original",
				"parts": []map[string]any{{"type": "text", "text": "new reply"}},
			}
			if tc.component {
				delete(body, "reply_to")
				body["parts"] = []map[string]any{{"type": "quote", "message_id": "original"}, {"type": "text", "text": "new reply"}}
			}
			raw, _ := json.Marshal(body)
			w := httptest.NewRecorder()
			s.messages(w, httptest.NewRequest("POST", "/v1/messages", bytes.NewReader(raw)))
			if w.Code != tc.wantStatus {
				t.Fatalf("status %d, want %d: %s", w.Code, tc.wantStatus, w.Body.String())
			}
			if tc.wantStatus == 422 && sends.Load() != 0 {
				t.Fatal("rejected quote was sent")
			}
			if (tc.wantStatus == 200 || tc.unknown) && sends.Load() != 1 {
				t.Fatal("quote was not sent exactly once")
			}
			if tc.missing && pulls.Load() != 4 {
				t.Fatal("history lookup must be bounded to 200 messages")
			}
			var result map[string]any
			json.Unmarshal(w.Body.Bytes(), &result)
			if tc.unknown && result["status"] != "unknown" {
				t.Fatal("lost receipt was falsely reported as success")
			}
			if tc.wantStatus == 200 && (result["status"] != "sent" || result["server_message_id"] != "quote-server") {
				t.Fatal("quote receipt missing")
			}
		})
	}
}

func TestInvalidQuotesNeverReachUpstream(t *testing.T) {
	s := &server{}
	for _, body := range []string{
		`{"kind":"group","target_id":"g","reply_to":" x","parts":[{"type":"text","text":"x"}]}`,
		`{"kind":"group","target_id":"g","parts":[{"type":"quote","message_id":123},{"type":"text","text":"x"}]}`,
		`{"kind":"group","target_id":"g","parts":[{"type":"quote","message_id":""},{"type":"text","text":"x"}]}`,
		`{"kind":"group","target_id":"g","reply_to":"one","parts":[{"type":"quote","message_id":"two"},{"type":"text","text":"x"}]}`,
		`{"kind":"group","target_id":"g","parts":[{"type":"quote","message_id":"one"},{"type":"quote","message_id":"one"},{"type":"text","text":"x"}]}`,
		`{"kind":"group","target_id":"g","parts":[{"type":"quote","message_id":"one"}]}`,
	} {
		w := httptest.NewRecorder()
		s.messages(w, httptest.NewRequest("POST", "/v1/messages", strings.NewReader(body)))
		if w.Code != 400 {
			t.Fatalf("invalid quote status %d: %s", w.Code, body)
		}
	}
}
