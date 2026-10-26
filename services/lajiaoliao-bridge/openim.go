package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/gob"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"github.com/openimsdk/protocol/sdkws"
	"google.golang.org/protobuf/proto"
)

type wireRequest struct {
	ReqIdentifier                       int
	Token, SendID, OperationID, MsgIncr string
	Data                                []byte
}
type wireResponse struct {
	ReqIdentifier, ErrCode       int
	ErrMsg, MsgIncr, OperationID string
	Data                         []byte
}

type openIM struct {
	mu           sync.Mutex
	conn         *websocket.Conn
	token        string
	user         string
	verified     bool
	state        string
	store        *tokenStore
	nextRefresh  time.Time
	nextConnect  time.Time
	connectDelay time.Duration
}

func newID() string {
	var value [16]byte
	if _, err := rand.Read(value[:]); err != nil {
		panic("secure random source unavailable")
	}
	return hex.EncodeToString(value[:])
}

func (o *openIM) status() (string, string) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.state == "" {
		return "disconnected", ""
	}
	return o.state, o.user
}

func (o *openIM) closeLocked() {
	if o.conn != nil {
		_ = o.conn.Close()
	}
	o.conn = nil
	o.verified = false
}

func (o *openIM) close() {
	o.mu.Lock()
	defer o.mu.Unlock()
	o.closeLocked()
	o.state = "disconnected"
}

func (o *openIM) backoffLocked() {
	if o.connectDelay == 0 {
		o.connectDelay = time.Second
	} else {
		o.connectDelay = min(30*time.Second, o.connectDelay*2)
	}
	o.nextConnect = time.Now().Add(o.connectDelay)
}

// RPCs are serialized. Push notifications are hints only: sequence polling is
// authoritative, so a reconnect cannot silently discard a pushed message.
func (o *openIM) rpc(ctx context.Context, code int, input, output proto.Message) error {
	o.mu.Lock()
	defer o.mu.Unlock()
	tokens, err := o.store.load()
	if err != nil || tokens.IMToken == "" {
		o.closeLocked()
		o.state = "logged_out"
		return errors.New("session_unavailable")
	}
	if o.token != tokens.IMToken {
		o.closeLocked()
		o.nextConnect = time.Time{}
		o.token, o.user = tokens.IMToken, tokens.UserID
		o.state = "disconnected"
	}
	if (o.state == "reauth_required" || o.state == "kicked_offline") && o.token == tokens.IMToken {
		return errors.New(o.state)
	}
	if o.conn == nil {
		if time.Now().Before(o.nextConnect) {
			return errors.New("upstream_reconnect_backoff")
		}
		if o.state == "kicked_offline" && o.token == tokens.IMToken {
			return errors.New("kicked_offline")
		}
		u, err := url.Parse(wsAddr())
		if err != nil {
			return errors.New("invalid_ws_address")
		}
		u.RawQuery = url.Values{"sendID": {tokens.UserID}, "token": {tokens.IMToken}, "platformID": {"5"}, "operationID": {newID()}, "isBackground": {"false"}}.Encode()
		dialer := websocket.Dialer{HandshakeTimeout: 10 * time.Second}
		conn, response, err := dialer.DialContext(ctx, u.String(), nil)
		if err != nil {
			o.state = "connection_failed"
			o.backoffLocked()
			if response != nil && (response.StatusCode == 401 || response.StatusCode == 403) {
				o.state = "reauth_required"
				o.token, o.user = tokens.IMToken, tokens.UserID
			}
			return errors.New(o.state)
		}
		o.conn, o.token, o.user = conn, tokens.IMToken, tokens.UserID
		o.conn.SetReadLimit(8 << 20)
		o.state = "connecting"
	}
	// Never send a message before the wire format has been verified.
	if code == 1003 && !o.verified {
		return errors.New("upstream_not_ready")
	}
	data, err := proto.Marshal(input)
	if err != nil {
		return errors.New("invalid_message")
	}
	id := newID()
	var buffer bytes.Buffer
	if err := gob.NewEncoder(&buffer).Encode(wireRequest{ReqIdentifier: code, SendID: o.user, OperationID: id, MsgIncr: id, Data: data}); err != nil {
		return errors.New("encode_failed")
	}
	deadline := time.Now().Add(12 * time.Second)
	if d, ok := ctx.Deadline(); ok && d.Before(deadline) {
		deadline = d
	}
	o.conn.SetWriteDeadline(deadline)
	o.conn.SetReadDeadline(deadline)
	if err := o.conn.WriteMessage(websocket.BinaryMessage, buffer.Bytes()); err != nil {
		o.closeLocked()
		o.state = "disconnected"
		o.backoffLocked()
		return errors.New("upstream_write_failed")
	}
	for {
		_, raw, err := o.conn.ReadMessage()
		if err != nil {
			o.closeLocked()
			o.state = "disconnected"
			o.backoffLocked()
			return errors.New("upstream_receipt_unavailable")
		}
		var result wireResponse
		if gob.NewDecoder(bytes.NewReader(raw)).Decode(&result) != nil {
			o.closeLocked()
			o.state = "protocol_incompatible"
			return errors.New(o.state)
		}
		if result.ReqIdentifier == 2002 {
			o.closeLocked()
			o.state = "kicked_offline"
			return errors.New(o.state)
		}
		if result.MsgIncr != id || result.ReqIdentifier != code {
			continue
		}
		if result.ErrCode != 0 {
			// OpenIM TokenExpiredError=1501; a generic HTTP 401 is not
			// evidence of expiration and must not renew or take over a session.
			if result.ErrCode == 1501 {
				o.closeLocked()
				o.state = "token_expired"
				if time.Now().Before(o.nextRefresh) {
					return errors.New("token_expired")
				}
				o.nextRefresh = time.Now().Add(30 * time.Second)
				if _, err := o.store.refresh(ctx, tokens); err != nil {
					if err.Error() == "reauth_required" {
						o.state = "reauth_required"
					}
					return err
				}
				o.nextRefresh = time.Time{}
				return errors.New("token_renewed")
			}
			if result.ErrCode >= 1502 && result.ErrCode <= 1507 {
				o.closeLocked()
				o.state = "reauth_required"
				if result.ErrCode == 1506 {
					o.state = "kicked_offline"
				}
				return errors.New(o.state)
			}
			if code == 1001 {
				o.verified = false
				o.state = "upstream_error"
			}
			return fmt.Errorf("upstream_error_%d", result.ErrCode)
		}
		if proto.Unmarshal(result.Data, output) != nil {
			o.closeLocked()
			o.state = "protocol_incompatible"
			return errors.New("protobuf_incompatible")
		}
		o.verified = true
		o.state = "online"
		o.connectDelay = 0
		o.nextConnect = time.Time{}
		return nil
	}
}

func (o *openIM) sequences(ctx context.Context) (*sdkws.GetMaxSeqResp, error) {
	tokens, err := o.store.load()
	if err != nil {
		return nil, errors.New("session_unavailable")
	}
	result := &sdkws.GetMaxSeqResp{}
	err = o.rpc(ctx, 1001, &sdkws.GetMaxSeqReq{UserID: tokens.UserID}, result)
	return result, err
}

func (o *openIM) run(ctx context.Context) {
	for {
		_, _ = o.sequences(ctx)
		select {
		case <-ctx.Done():
			o.close()
			return
		case <-time.After(15 * time.Second):
		}
	}
}

func textMessage(data *sdkws.MsgData, self string) map[string]any {
	if data.SendID == self || data.ClientMsgID == "" ||
		(data.ContentType != 101 && data.ContentType != 102 && data.ContentType != 114) {
		return nil
	}
	if data.SessionType != 1 && data.SessionType != 2 && data.SessionType != 3 {
		return nil
	}
	text, quote, err := textualContent(data)
	var picture pictureContent
	if data.ContentType == 102 {
		err = json.Unmarshal(data.Content, &picture)
		if err != nil || !imageURL(picture.Source.URL) || picture.Source.Size <= 0 ||
			picture.Source.Size > maxImageBytes || picture.Source.Width <= 0 || picture.Source.Height <= 0 ||
			int64(picture.Source.Width)*int64(picture.Source.Height) > maxImagePixels ||
			(picture.Source.Type != "jpg" && picture.Source.Type != "jpeg" && picture.Source.Type != "png" &&
				picture.Source.Type != "gif" && picture.Source.Type != "image/jpeg" &&
				picture.Source.Type != "image/png" && picture.Source.Type != "image/gif") {
			return nil
		}
		text, quote = "", nil
	}
	if err != nil {
		return nil
	}
	if quote != nil && quote.ContentType >= 102 && quote.ContentType <= 106 {
		return nil
	}
	target, kind := data.SendID, "private"
	if data.SessionType == 2 || data.SessionType == 3 {
		if data.GroupID == "" {
			return nil
		}
		target, kind = data.GroupID, "group"
	}
	event := map[string]any{"message_id": data.ClientMsgID, "server_message_id": data.ServerMsgID, "sender_id": data.SendID, "sender_name": data.SenderNickname, "target_id": target, "kind": kind, "text": text, "timestamp": data.SendTime / 1000}
	if data.ContentType == 102 {
		event["components"] = []map[string]any{{"type": "image", "url": picture.Source.URL}}
	}
	if quote != nil {
		quotedText := ""
		if quote.TextElem != nil {
			quotedText = quote.TextElem.Content
		}
		event["components"] = []map[string]any{{
			"type": "quote", "message_id": quote.ClientMsgID,
			"sender_id": quote.SendID, "sender_name": quote.SenderNickname,
			"text": quotedText, "timestamp": quote.SendTime / 1000,
		}}
	}
	return event
}

func unsupportedMediaMessage(data *sdkws.MsgData, self string) map[string]any {
	if data == nil || data.SendID == self || data.MsgFrom != 100 || data.ClientMsgID == "" ||
		((data.ContentType < 102 || data.ContentType > 106) && data.ContentType != 114) {
		return nil
	}
	if data.ContentType == 114 {
		_, quote, err := textualContent(data)
		if err != nil || quote == nil || quote.ContentType < 102 || quote.ContentType > 106 {
			return nil
		}
	}
	target, kind := data.SendID, "private"
	if data.SessionType == 2 || data.SessionType == 3 {
		if data.GroupID == "" {
			return nil
		}
		target, kind = data.GroupID, "group"
	} else if data.SessionType != 1 || data.RecvID != self {
		return nil
	}
	return map[string]any{
		"message_id": data.ClientMsgID, "action": "unsupported_media",
		"kind": kind, "target_id": target, "group_id": data.GroupID,
		"sender_id": data.SendID, "content_type": data.ContentType,
		"status": "unsupported", "timestamp": data.SendTime / 1000,
	}
}
