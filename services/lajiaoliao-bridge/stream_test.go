package main

import (
	"bytes"
	"encoding/gob"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	"github.com/openimsdk/protocol/sdkws"
	"google.golang.org/protobuf/proto"
)

func TestStreamReplaysUnacknowledgedAndPersistsAck(t *testing.T) {
	for _, notification := range []bool{false, true} {
		t.Run(fmt.Sprint("notification=", notification), func(t *testing.T) {
			testStreamAck(t, notification)
		})
	}
}

func testStreamAck(t *testing.T, notification bool) {
	dir := t.TempDir()
	store := newTokenStore(filepath.Join(dir, "token.json"))
	if err := store.save(tokenFile{UserID: "bot", IMToken: "mock", ChatToken: "mock"}); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "cursor.json")
	conversation := "si_bot_user"
	if notification {
		conversation = "n_group"
	}
	if saveCursor(path, cursorFile{UserID: "bot", Seqs: map[string]int64{conversation: 0}}) != nil {
		t.Fatal("save cursor")
	}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := (&websocket.Upgrader{}).Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer c.Close()
		for {
			_, raw, err := c.ReadMessage()
			if err != nil {
				return
			}
			var req wireRequest
			if gob.NewDecoder(bytes.NewReader(raw)).Decode(&req) != nil {
				return
			}
			var result proto.Message
			switch req.ReqIdentifier {
			case 1001:
				result = &sdkws.GetMaxSeqResp{MaxSeqs: map[string]int64{conversation: 1}}
			case 1002:
				message := &sdkws.MsgData{
					ClientMsgID: "mock-message", SendID: "user", RecvID: "bot", ContentType: 101, SessionType: 1, Content: []byte(`{"content":"hello"}`), Seq: 1,
				}
				batch := &sdkws.PullMessageBySeqsResp{Msgs: map[string]*sdkws.PullMsgs{conversation: {Msgs: []*sdkws.MsgData{message}}}}
				if notification {
					message.ContentType, message.MsgFrom, message.GroupID, message.SessionType, message.RecvID = 2101, 200, "group", 3, "group"
					message.Content = []byte(`{"detail":"{\"clientMsgID\":\"original\",\"revokerUserID\":\"user\",\"conversationID\":\"sg_group\",\"sesstionType\":3}"}`)
					batch.NotificationMsgs, batch.Msgs = batch.Msgs, nil
				}
				result = batch
			default:
				t.Error("unexpected RPC")
				return
			}
			data, _ := proto.Marshal(result)
			var buf bytes.Buffer
			gob.NewEncoder(&buf).Encode(wireResponse{ReqIdentifier: req.ReqIdentifier, MsgIncr: req.MsgIncr, Data: data})
			if c.WriteMessage(websocket.BinaryMessage, buf.Bytes()) != nil {
				return
			}
		}
	}))
	defer upstream.Close()
	t.Setenv("LAJIAOLIAO_WS_ADDR", "ws"+strings.TrimPrefix(upstream.URL, "http"))
	o := &openIM{store: store}
	defer o.close()
	s := &server{tokens: store, upstream: o}
	local := httptest.NewServer(s.routes())
	defer local.Close()
	connect := func() *websocket.Conn {
		t.Helper()
		deadline := time.Now().Add(2 * time.Second)
		for time.Now().Before(deadline) {
			c, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(local.URL, "http")+"/v1/events", nil)
			if err == nil {
				c.SetReadDeadline(time.Now().Add(3 * time.Second))
				return c
			}
			time.Sleep(10 * time.Millisecond)
		}
		t.Fatal("stream connection failed")
		return nil
	}
	readMessage := func(c *websocket.Conn) {
		t.Helper()
		for {
			var v struct {
				Type string `json:"type"`
				Data struct {
					MessageID string `json:"message_id"`
				} `json:"data"`
			}
			if err := c.ReadJSON(&v); err != nil {
				t.Fatal("event not received")
			}
			wanted := "message"
			if notification {
				wanted = "notification"
			}
			if v.Type == wanted {
				if v.Data.MessageID != "mock-message" {
					t.Fatal("bad event")
				}
				return
			}
		}
	}
	first := connect()
	readMessage(first)
	first.Close()
	second := connect()
	defer second.Close()
	readMessage(second)
	if err := second.WriteJSON(map[string]string{"type": "ack", "message_id": "mock-message"}); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		raw, _ := os.ReadFile(path)
		var cursor cursorFile
		json.Unmarshal(raw, &cursor)
		if cursor.Seqs[conversation] == 1 {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("acknowledged sequence was not persisted")
}
