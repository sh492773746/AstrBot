package main

import (
	"bytes"
	"context"
	"encoding/gob"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gorilla/websocket"
	msgproto "github.com/openimsdk/protocol/msg"
	"github.com/openimsdk/protocol/sdkws"
	"google.golang.org/protobuf/proto"
)

func TestOpenIMWireAndTextReceipt(t *testing.T) {
	store := newTokenStore(filepath.Join(t.TempDir(), "token.json"))
	if err := store.save(tokenFile{UserID: "bot", IMToken: "mock-im", ChatToken: "mock-chat"}); err != nil {
		t.Fatal(err)
	}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("token") != "mock-im" || r.URL.Query().Get("platformID") != "5" {
			t.Error("invalid connection credentials")
		}
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
				t.Error("bad gob")
				return
			}
			var out proto.Message
			switch req.ReqIdentifier {
			case 1001:
				var input sdkws.GetMaxSeqReq
				if proto.Unmarshal(req.Data, &input) != nil || input.UserID != "bot" {
					t.Error("bad sequence request")
				}
				out = &sdkws.GetMaxSeqResp{MaxSeqs: map[string]int64{"si_bot_user": 10}}
			case 1003:
				var input sdkws.MsgData
				if proto.Unmarshal(req.Data, &input) != nil {
					t.Error("bad message")
				}
				if input.SessionType != 1 || input.RecvID != "user" || input.SendID != "bot" || input.ContentType != 101 {
					t.Error("bad message fields")
				}
				out = &msgproto.SendMsgResp{ClientMsgID: input.ClientMsgID, ServerMsgID: "server-1", SendTime: 123}
			default:
				t.Error("unexpected request")
				return
			}
			data, _ := proto.Marshal(out)
			var buffer bytes.Buffer
			gob.NewEncoder(&buffer).Encode(wireResponse{ReqIdentifier: req.ReqIdentifier, MsgIncr: req.MsgIncr, Data: data})
			conn.WriteMessage(websocket.BinaryMessage, buffer.Bytes())
		}
	}))
	defer upstream.Close()
	t.Setenv("LAJIAOLIAO_WS_ADDR", "ws"+strings.TrimPrefix(upstream.URL, "http"))
	o := &openIM{store: store}
	defer o.close()
	seq, err := o.sequences(context.Background())
	if err != nil || seq.MaxSeqs["si_bot_user"] != 10 {
		t.Fatal("sequence RPC failed")
	}
	state, user := o.status()
	if state != "online" || user != "bot" {
		t.Fatal("incorrect connection state")
	}
	s := &server{tokens: store, upstream: o}
	w := httptest.NewRecorder()
	s.messages(w, httptest.NewRequest("POST", "/v1/messages", strings.NewReader(`{"kind":"private","target_id":"user","parts":[{"type":"text","text":"mock"}]}`)))
	if w.Code != 200 {
		t.Fatalf("send status %d", w.Code)
	}
	var result map[string]any
	json.Unmarshal(w.Body.Bytes(), &result)
	if result["status"] != "sent" || result["server_message_id"] != "server-1" {
		t.Fatal("receipt missing")
	}
	o.close()
	if _, err := o.sequences(context.Background()); err != nil {
		t.Fatal("reconnect failed")
	}
}

func TestUnsupportedMessageNeverSent(t *testing.T) {
	s := &server{}
	for _, body := range []string{
		`{"kind":"private","target_id":"user","parts":[{"type":"image","url":"x"}]}`,
		`{"kind":"private","target_id":"user","parts":[{"type":"mention","user_id":"u1"}]}`,
	} {
		w := httptest.NewRecorder()
		s.messages(w, httptest.NewRequest("POST", "/v1/messages", strings.NewReader(body)))
		if w.Code != 422 {
			t.Fatalf("status %d, expected unsupported", w.Code)
		}
	}
}

func TestTextMappingAndCursor(t *testing.T) {
	m := &sdkws.MsgData{SendID: "user", ClientMsgID: "m1", ContentType: 101, SessionType: 3, GroupID: "g1", Content: []byte(`{"content":"hello"}`), SendTime: 12000}
	result := textMessage(m, "bot")
	if result["target_id"] != "g1" || result["kind"] != "group" || result["timestamp"] != int64(12) {
		t.Fatal("incorrect group mapping")
	}
	if textMessage(m, "user") != nil {
		t.Fatal("self message must be ignored")
	}
	m.ContentType = 102
	if textMessage(m, "bot") != nil {
		t.Fatal("unsupported message must not produce a false text event")
	}
	path := filepath.Join(t.TempDir(), "cursor.json")
	if saveCursor(path, cursorFile{UserID: "bot", Seqs: map[string]int64{"g1": 9}}) != nil {
		t.Fatal("cursor save failed")
	}
}
