package main

import (
	"context"
	"encoding/json"
	"errors"
	"sort"
	"strings"
	"time"

	"github.com/openimsdk/protocol/sdkws"
)

// This is the SDK message shape, not the protobuf wire envelope. Only verified
// textual content is embedded; quoting another quote flattens its own text.
type quotedMessage struct {
	ClientMsgID      string `json:"clientMsgID"`
	ServerMsgID      string `json:"serverMsgID"`
	CreateTime       int64  `json:"createTime"`
	SendTime         int64  `json:"sendTime"`
	SessionType      int32  `json:"sessionType"`
	SendID           string `json:"sendID"`
	RecvID           string `json:"recvID"`
	GroupID          string `json:"groupID,omitempty"`
	MsgFrom          int32  `json:"msgFrom"`
	ContentType      int32  `json:"contentType"`
	SenderPlatformID int32  `json:"senderPlatformID"`
	SenderNickname   string `json:"senderNickname"`
	SenderFaceURL    string `json:"senderFaceUrl,omitempty"`
	Seq              int64  `json:"seq"`
	Status           int    `json:"status"`
	TextElem         *struct {
		Content string `json:"content"`
	} `json:"textElem,omitempty"`
}

type quoteContent struct {
	Text         string         `json:"text"`
	QuoteMessage *quotedMessage `json:"quoteMessage"`
}

func textualContent(data *sdkws.MsgData) (string, *quotedMessage, error) {
	switch data.ContentType {
	case 101:
		var content struct {
			Content string `json:"content"`
		}
		if json.Unmarshal(data.Content, &content) != nil || content.Content == "" {
			return "", nil, errors.New("invalid_text_content")
		}
		return content.Content, nil, nil
	case 114:
		var content quoteContent
		if json.Unmarshal(data.Content, &content) != nil || content.Text == "" ||
			content.QuoteMessage == nil || content.QuoteMessage.ClientMsgID == "" {
			return "", nil, errors.New("invalid_quote_content")
		}
		q := content.QuoteMessage
		if len(q.ClientMsgID) > 128 || q.SendTime < 0 ||
			(data.SessionType == 1 && (q.SessionType != 1 ||
				!((q.SendID == data.SendID && q.RecvID == data.RecvID) ||
					(q.SendID == data.RecvID && q.RecvID == data.SendID)))) ||
			(data.SessionType != 1 && (q.GroupID != data.GroupID ||
				(q.SessionType != 2 && q.SessionType != 3))) {
			return "", nil, errors.New("quote_conversation_mismatch")
		}
		return content.Text, q, nil
	default:
		return "", nil, errors.New("unsupported_quote_target")
	}
}

// Fetch through the existing connection, without a second consumer or cursor
// changes. A bounded history lookup survives restarts without storing bodies.
func (s *server) resolveQuote(ctx context.Context, self, kind, target, id string) (*sdkws.MsgData, error) {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	sequences, err := s.upstream.sequences(ctx)
	if err != nil {
		return nil, errors.New("quote_lookup_failed")
	}
	conversations := []string{"sg_" + target, "g_" + target}
	if kind == "private" {
		users := []string{self, target}
		sort.Strings(users)
		conversations = []string{"si_" + strings.Join(users, "_")}
	}
	for _, conversation := range conversations {
		end := sequences.MaxSeqs[conversation]
		floor := max(int64(1), end-199, sequences.MinSeqs[conversation])
		for end >= floor {
			begin := max(floor, end-49)
			var batch sdkws.PullMessageBySeqsResp
			err := s.upstream.rpc(ctx, 1002, &sdkws.PullMessageBySeqsReq{
				UserID: self,
				SeqRanges: []*sdkws.SeqRange{{
					ConversationID: conversation, Begin: begin, End: end, Num: 50,
				}},
			}, &batch)
			if err != nil {
				return nil, errors.New("quote_lookup_failed")
			}
			if messages := batch.Msgs[conversation]; messages != nil {
				for _, message := range messages.Msgs {
					if message.ClientMsgID != id || message.Seq < begin || message.Seq > end {
						continue
					}
					if (kind == "group" && (message.GroupID != target ||
						(message.SessionType != 2 && message.SessionType != 3))) ||
						(kind == "private" && (message.SessionType != 1 ||
							!((message.SendID == self && message.RecvID == target) ||
								(message.SendID == target && message.RecvID == self)))) {
						return nil, errors.New("quote_conversation_mismatch")
					}
					return message, nil
				}
			}
			end = begin - 1
		}
	}
	return nil, errors.New("quote_not_found")
}

func quoteEnvelope(text string, original *sdkws.MsgData) ([]byte, error) {
	originalText, _, err := textualContent(original)
	if err != nil {
		return nil, err
	}
	q := &quotedMessage{
		ClientMsgID: original.ClientMsgID, ServerMsgID: original.ServerMsgID,
		CreateTime: original.CreateTime, SendTime: original.SendTime,
		SessionType: original.SessionType, SendID: original.SendID,
		RecvID: original.RecvID, GroupID: original.GroupID,
		MsgFrom: original.MsgFrom, ContentType: 101,
		SenderPlatformID: original.SenderPlatformID,
		SenderNickname:   original.SenderNickname, SenderFaceURL: original.SenderFaceURL,
		Seq: original.Seq, Status: 2,
	}
	q.TextElem = &struct {
		Content string `json:"content"`
	}{Content: originalText}
	return json.Marshal(quoteContent{Text: text, QuoteMessage: q})
}
