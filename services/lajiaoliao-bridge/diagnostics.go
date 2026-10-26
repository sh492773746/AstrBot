package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"

	"github.com/openimsdk/protocol/sdkws"
)

// Preserve protocol keys, types and dimensions, never signed media addresses.
func diagnosticShape(value any, key string, depth int) any {
	if depth > 8 {
		return "depth_limit"
	}
	switch value := value.(type) {
	case map[string]any:
		out := make(map[string]any)
		for name, item := range value {
			out[name] = diagnosticShape(item, name, depth+1)
		}
		return out
	case []any:
		out := []any{}
		for _, item := range value[:min(len(value), 20)] {
			out = append(out, diagnosticShape(item, key, depth+1))
		}
		return out
	case string:
		u, err := url.Parse(value)
		if err == nil && (u.Scheme == "https" || u.Scheme == "http") && u.Hostname() != "" {
			digest := sha256.Sum256([]byte(value))
			return map[string]any{"scheme": u.Scheme, "host": u.Hostname(), "sha256": hex.EncodeToString(digest[:]), "has_query": u.RawQuery != ""}
		}
		if key == "type" && len(value) <= 64 {
			return value
		}
		return map[string]any{"type": "string", "bytes": len(value)}
	case float64, bool, nil:
		return value
	default:
		return "unknown"
	}
}

func diagnosticMessage(message *sdkws.MsgData, self, kind, target string) map[string]any {
	if message == nil || message.ClientMsgID == "" || message.MsgFrom != 100 {
		return nil
	}
	if kind == "group" {
		if message.GroupID != target || (message.SessionType != 2 && message.SessionType != 3) {
			return nil
		}
	} else if message.SessionType != 1 || !((message.SendID == self && message.RecvID == target) ||
		(message.SendID == target && message.RecvID == self)) {
		return nil
	}
	row := map[string]any{
		"message_id": message.ClientMsgID, "sender_id": message.SendID, "seq": message.Seq,
		"timestamp": message.SendTime / 1000, "content_type": message.ContentType,
		"session_type": message.SessionType, "sender_platform": message.SenderPlatformID,
	}
	if message.ContentType == 102 {
		row["adapter_convertible"] = textMessage(message, self) != nil
	}
	if len(message.Content) > 65536 {
		row["content_error"] = "content_too_large"
		return row
	}
	if message.ContentType == 101 || message.ContentType == 114 {
		text, quoted, err := textualContent(message)
		if err == nil {
			runes := []rune(text)
			row["text"] = string(runes[:min(len(runes), 4096)])
			if quoted != nil {
				row["quoted_message_id"] = quoted.ClientMsgID
			}
		}
	} else {
		var content any
		if json.Unmarshal(message.Content, &content) == nil {
			row["content_shape"] = diagnosticShape(content, "", 0)
		} else {
			row["content_error"] = "invalid_json"
		}
	}
	return row
}

func (s *server) inspectMessages(w http.ResponseWriter, r *http.Request) {
	kind, target := r.URL.Query().Get("kind"), r.URL.Query().Get("target_id")
	if (kind != "group" && kind != "private") || target == "" ||
		len(target) > 128 || strings.TrimSpace(target) != target {
		writeError(w, 400, "invalid_inspection_target")
		return
	}
	tokens, err := s.tokens.load()
	if err != nil || tokens.IMToken == "" || s.upstream == nil {
		writeError(w, 409, "session_unavailable")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	if kind == "group" {
		if _, err := selfGroupMember(ctx, tokens, target); err != nil {
			writeError(w, 403, "group_membership_unverified")
			return
		}
	}
	sequences, err := s.upstream.sequences(ctx)
	if err != nil {
		writeError(w, 503, "history_unavailable")
		return
	}
	conversations := []string{"sg_" + target, "g_" + target}
	if kind == "private" {
		users := []string{tokens.UserID, target}
		sort.Strings(users)
		conversations = []string{"si_" + strings.Join(users, "_")}
	}
	rows := []map[string]any{}
	seen := map[string]bool{}
	for _, conversation := range conversations {
		end := sequences.MaxSeqs[conversation]
		if end < 1 {
			continue
		}
		var batch sdkws.PullMessageBySeqsResp
		begin := max(int64(1), end-99, sequences.MinSeqs[conversation])
		if begin > end {
			continue
		}
		if err := s.upstream.rpc(ctx, 1002, &sdkws.PullMessageBySeqsReq{
			UserID: tokens.UserID, SeqRanges: []*sdkws.SeqRange{{
				ConversationID: conversation, Begin: begin, End: end, Num: 100,
			}},
		}, &batch); err != nil {
			writeError(w, 503, "history_unavailable")
			return
		}
		if history := batch.Msgs[conversation]; history != nil {
			for _, message := range history.Msgs {
				if message.Seq < begin || message.Seq > end || seen[message.ClientMsgID] {
					continue
				}
				if row := diagnosticMessage(message, tokens.UserID, kind, target); row != nil {
					seen[message.ClientMsgID] = true
					rows = append(rows, row)
				}
			}
		}
	}
	sort.Slice(rows, func(i, j int) bool { return rows[i]["timestamp"].(int64) < rows[j]["timestamp"].(int64) })
	if len(rows) > 100 {
		rows = rows[len(rows)-100:]
	}
	writeJSON(w, 200, map[string]any{"kind": kind, "target_id": target, "messages": rows})
}
