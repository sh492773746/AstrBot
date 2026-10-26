package main

import (
	"bytes"
	"context"
	"encoding/gob"
	"fmt"
	"net/url"
	"os"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	"github.com/openimsdk/protocol/sdkws"
	"google.golang.org/protobuf/proto"
)

// Explicit opt-in: reads only sequence metadata, never sends a chat message.
func TestLiveOpenIMReadOnly(t *testing.T) {
	path := os.Getenv("LAJIAOLIAO_LIVE_TOKEN_FILE")
	if path == "" {
		t.Skip("live probe disabled")
	}
	tokens, err := newTokenStore(path).load()
	if err != nil || tokens.IMToken == "" {
		t.Fatal("session unavailable")
	}
	u, _ := url.Parse(wsAddr())
	q := url.Values{"sendID": {tokens.UserID}, "token": {tokens.IMToken}, "platformID": {"5"}, "operationID": {"bridge-readonly-probe"}, "isBackground": {"false"}}
	u.RawQuery = q.Encode()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	conn, resp, err := websocket.DefaultDialer.DialContext(ctx, u.String(), nil)
	if err != nil {
		if resp != nil {
			t.Fatalf("handshake failed: HTTP %d", resp.StatusCode)
		}
		t.Fatal("handshake failed (details redacted)")
	}
	defer conn.Close()
	conn.SetReadLimit(8 << 20)
	conn.SetReadDeadline(time.Now().Add(15 * time.Second))
	data, _ := proto.Marshal(&sdkws.GetMaxSeqReq{UserID: tokens.UserID})
	request := struct {
		ReqIdentifier                       int
		Token, SendID, OperationID, MsgIncr string
		Data                                []byte
	}{ReqIdentifier: 1001, SendID: tokens.UserID, OperationID: "bridge-readonly-probe", MsgIncr: "probe-1", Data: data}
	var buffer bytes.Buffer
	if err := gob.NewEncoder(&buffer).Encode(request); err != nil {
		t.Fatal("encode failed")
	}
	if err := conn.WriteMessage(websocket.BinaryMessage, buffer.Bytes()); err != nil {
		t.Fatal("write failed")
	}
	for {
		kind, raw, err := conn.ReadMessage()
		if err != nil {
			t.Fatal("protocol response unavailable (details redacted)")
		}
		var result struct {
			ReqIdentifier                int
			ErrCode                      int
			ErrMsg, MsgIncr, OperationID string
			Data                         []byte
		}
		if err := gob.NewDecoder(bytes.NewReader(raw)).Decode(&result); err != nil {
			t.Fatalf("gob incompatible: frame type=%d length=%d", kind, len(raw))
		}
		if result.ReqIdentifier != 1001 {
			continue
		}
		if result.ErrCode != 0 {
			t.Fatalf("sequence response error code=%d", result.ErrCode)
		}
		var seq sdkws.GetMaxSeqResp
		if err := proto.Unmarshal(result.Data, &seq); err != nil {
			t.Fatal("protobuf schema incompatible")
		}
		fmt.Printf("OpenIM verified: handshake=true gob=true protobuf=true conversations=%d\n", len(seq.MaxSeqs))
		return
	}
}

func TestLiveOpenIMPullSchema(t *testing.T) {
	path := os.Getenv("LAJIAOLIAO_LIVE_TOKEN_FILE")
	if path == "" {
		t.Skip("live probe disabled")
	}
	store := newTokenStore(path)
	tokens, err := store.load()
	if err != nil {
		t.Fatal("session unavailable")
	}
	o := &openIM{store: store}
	defer o.close()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	seq, err := o.sequences(ctx)
	if err != nil {
		t.Fatal("sequence probe failed")
	}
	for conversation, end := range seq.MaxSeqs {
		if end < 1 {
			continue
		}
		var result sdkws.PullMessageBySeqsResp
		if err := o.rpc(ctx, 1002, &sdkws.PullMessageBySeqsReq{UserID: tokens.UserID, SeqRanges: []*sdkws.SeqRange{{ConversationID: conversation, Begin: end, End: end, Num: 1}}}, &result); err != nil {
			t.Fatal("pull schema probe failed")
		}
		count := 0
		for _, batch := range result.Msgs {
			count += len(batch.Msgs)
		}
		for _, batch := range result.NotificationMsgs {
			count += len(batch.Msgs)
		}
		fmt.Printf("OpenIM read-only pull verified: decoded_messages=%d (content not logged)\n", count)
		return
	}
	t.Log("no nonempty conversation available for pull verification")
}
