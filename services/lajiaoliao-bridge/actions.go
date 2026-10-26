package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/openimsdk/protocol/sdkws"
)

type actionRequest struct {
	OperationID          string  `json:"operation_id"`
	Action               string  `json:"action"`
	Kind                 string  `json:"kind"`
	TargetID             string  `json:"target_id"`
	MessageID            string  `json:"message_id,omitempty"`
	Nickname             string  `json:"nickname,omitempty"`
	ExpectedNickname     *string `json:"expected_nickname,omitempty"`
	UserID               string  `json:"user_id,omitempty"`
	MutedSeconds         uint32  `json:"muted_seconds,omitempty"`
	Announcement         string  `json:"announcement,omitempty"`
	ExpectedAnnouncement *string `json:"expected_announcement,omitempty"`
	ConfirmKick          bool    `json:"confirm_kick,omitempty"`
}

type actionReceipt struct {
	Digest      string `json:"digest,omitempty"`
	OperationID string `json:"operation_id"`
	Status      string `json:"status"`
	Error       string `json:"error,omitempty"`
	Action      string `json:"action"`
	Kind        string `json:"kind"`
	TargetID    string `json:"target_id"`
	Timestamp   int64  `json:"timestamp"`
}

type actionLedger struct {
	UserID   string                   `json:"user_id"`
	Receipts map[string]actionReceipt `json:"receipts"`
}

type groupMember struct {
	GroupID     string `json:"groupID"`
	UserID      string `json:"userID"`
	Nickname    string `json:"nickname"`
	RoleLevel   int32  `json:"roleLevel"`
	MuteEndTime int64  `json:"muteEndTime"`
}

func selfGroupMember(ctx context.Context, tokens tokenFile, group string) (groupMember, error) {
	return verifiedGroupMember(ctx, tokens, group, tokens.UserID)
}

func verifiedGroupMember(ctx context.Context, tokens tokenFile, group, uid string) (groupMember, error) {
	raw, err := postJSON(ctx, apiAddr()+"/group/get_group_members_info",
		map[string]any{"groupID": group, "userIDs": []string{uid}}, tokens.IMToken)
	var response struct {
		Data struct {
			Members []groupMember `json:"members"`
		} `json:"data"`
	}
	if err != nil || json.Unmarshal(raw, &response) != nil || len(response.Data.Members) != 1 ||
		response.Data.Members[0].UserID != uid || response.Data.Members[0].GroupID != group {
		return groupMember{}, errors.New("group_membership_unverified")
	}
	return response.Data.Members[0], nil
}

var operationPattern = regexp.MustCompile(`^[a-zA-Z0-9_-]{8,128}$`)

func privateFile(path string, data []byte) error {
	f, err := os.CreateTemp(filepath.Dir(path), ".action-*")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if _, err = f.Write(data); err != nil {
		f.Close()
		return err
	}
	if err = f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	if err = os.Rename(f.Name(), path); err != nil {
		return err
	}
	dir, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

// Fixed, scoped mutations. A durable unknown receipt is written BEFORE the
// upstream request; losing the response never permits an automatic repeat.
func (s *server) actions(w http.ResponseWriter, r *http.Request) {
	var input actionRequest
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 16384))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&input) != nil || !operationPattern.MatchString(input.OperationID) ||
		input.TargetID == "" || len(input.TargetID) > 128 ||
		strings.TrimSpace(input.TargetID) != input.TargetID {
		writeError(w, 400, "invalid_action_request")
		return
	}
	native := nativeAction(input.Action)
	memberEdit := input.Action == "revoke_member_message" || input.Action == "set_member_group_nickname"
	if input.Action != "revoke_self_message" && input.Action != "set_self_group_nickname" && !native && !memberEdit {
		writeJSON(w, 422, map[string]string{"status": "unsupported", "error": "unsupported_action"})
		return
	}
	if native {
		if err := validateNativeAction(input); err != nil {
			writeError(w, 400, err.Error())
			return
		}
	} else if (input.UserID != "" && !memberEdit) || input.MutedSeconds != 0 || input.Announcement != "" ||
		input.ExpectedAnnouncement != nil || input.ConfirmKick {
		writeError(w, 400, "unexpected_action_fields")
		return
	}
	if memberEdit && (input.Kind != "group" || input.UserID == "" || len(input.UserID) > 128 ||
		strings.TrimSpace(input.UserID) != input.UserID ||
		(input.Action == "revoke_member_message" && (input.MessageID == "" || len(input.MessageID) > 128 ||
			strings.TrimSpace(input.MessageID) != input.MessageID || input.Nickname != "" || input.ExpectedNickname != nil)) ||
		(input.Action == "set_member_group_nickname" && (input.MessageID != "" || input.ExpectedNickname == nil ||
			input.Nickname == "" || strings.TrimSpace(input.Nickname) != input.Nickname ||
			utf8.RuneCountInString(input.Nickname) > 15 || strings.IndexFunc(input.Nickname, unicode.IsControl) >= 0))) {
		writeError(w, 400, "invalid_action_request")
		return
	}
	if (input.Kind != "group" && input.Kind != "private") ||
		(input.Action == "revoke_self_message" && (input.MessageID == "" ||
			len(input.MessageID) > 128 || strings.TrimSpace(input.MessageID) != input.MessageID ||
			input.Nickname != "" || input.ExpectedNickname != nil)) ||
		(input.Action == "set_self_group_nickname" && (input.Kind != "group" ||
			input.MessageID != "" || input.ExpectedNickname == nil ||
			input.Nickname == "" || strings.TrimSpace(input.Nickname) != input.Nickname ||
			utf8.RuneCountInString(input.Nickname) > 15 ||
			strings.IndexFunc(input.Nickname, unicode.IsControl) >= 0)) {
		writeError(w, 400, "invalid_action_request")
		return
	}
	s.actionMu.Lock()
	defer s.actionMu.Unlock()
	tokens, err := s.tokens.load()
	if err != nil || tokens.IMToken == "" || s.upstream == nil {
		writeError(w, 409, "session_unavailable")
		return
	}
	path := filepath.Join(filepath.Dir(s.tokens.path), "actions.json")
	ledger := actionLedger{UserID: tokens.UserID, Receipts: map[string]actionReceipt{}}
	raw, err := os.ReadFile(path)
	if err == nil {
		if json.Unmarshal(raw, &ledger) != nil || ledger.UserID != tokens.UserID || ledger.Receipts == nil {
			writeError(w, 409, "action_ledger_invalid")
			return
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		writeError(w, 503, "action_ledger_unavailable")
		return
	}
	body, _ := json.Marshal(input)
	hash := sha256.Sum256(body)
	digest := hex.EncodeToString(hash[:])
	if old, ok := ledger.Receipts[input.OperationID]; ok {
		if old.Digest != digest {
			writeError(w, 409, "operation_conflict")
			return
		}
		old.Digest = ""
		writeJSON(w, 200, old)
		return
	}
	// Never evict unknown receipts: old IDs must remain non-repeatable.
	if len(ledger.Receipts) >= 4096 {
		writeError(w, 409, "action_ledger_full")
		return
	}
	state, user := s.upstream.status()
	if state != "online" || user != tokens.UserID {
		writeError(w, 409, "instance_offline")
		return
	}
	var route string
	var payload any
	var targetMember groupMember
	if memberEdit {
		self, lookupErr := selfGroupMember(r.Context(), tokens, input.TargetID)
		if lookupErr != nil || (self.RoleLevel != 60 && self.RoleLevel != 100) || input.UserID == tokens.UserID {
			writeJSON(w, 403, map[string]string{"status": "rejected", "error": "native_admin_required"})
			return
		}
		raw, lookupErr := postJSON(r.Context(), apiAddr()+"/group/get_group_members_info",
			map[string]any{"groupID": input.TargetID, "userIDs": []string{input.UserID}}, tokens.IMToken)
		var people struct {
			Data struct {
				Members []groupMember `json:"members"`
			} `json:"data"`
		}
		if lookupErr != nil || json.Unmarshal(raw, &people) != nil || len(people.Data.Members) != 1 ||
			people.Data.Members[0].GroupID != input.TargetID || people.Data.Members[0].UserID != input.UserID ||
			people.Data.Members[0].RoleLevel != 20 {
			writeJSON(w, 403, map[string]string{"status": "rejected", "error": "protected_or_unverified_member"})
			return
		}
		targetMember = people.Data.Members[0]
	}
	if native {
		var nativeErr error
		route, payload, nativeErr = nativeActionPayload(r.Context(), tokens, input)
		if nativeErr != nil {
			writeJSON(w, 403, map[string]string{"status": "rejected", "error": nativeErr.Error()})
			return
		}
	} else if input.Action == "revoke_self_message" || input.Action == "revoke_member_message" {
		message, lookupErr := s.resolveQuote(r.Context(), tokens.UserID, input.Kind, input.TargetID, input.MessageID)
		if lookupErr != nil {
			writeJSON(w, 422, map[string]string{"status": "rejected", "error": lookupErr.Error()})
			return
		}
		expected := tokens.UserID
		if input.Action == "revoke_member_message" {
			expected = input.UserID
		}
		if message.SendID != expected || message.ContentType == 2101 || message.Seq <= 0 {
			writeJSON(w, 403, map[string]string{"status": "rejected", "error": "own_unrevoked_message_required"})
			return
		}
		conversation := "sg_" + input.TargetID
		if message.SessionType == 2 {
			conversation = "g_" + input.TargetID
		} else if input.Kind == "private" {
			if tokens.UserID < input.TargetID {
				conversation = "si_" + tokens.UserID + "_" + input.TargetID
			} else {
				conversation = "si_" + input.TargetID + "_" + tokens.UserID
			}
		}
		route = "/msg/revoke_msg"
		payload = map[string]any{"conversationID": conversation, "seq": message.Seq, "userID": tokens.UserID}
	} else {
		member, lookupErr := targetMember, error(nil)
		if input.Action == "set_self_group_nickname" {
			member, lookupErr = selfGroupMember(r.Context(), tokens, input.TargetID)
		}
		if lookupErr != nil {
			writeJSON(w, 422, map[string]string{"status": "rejected", "error": "group_membership_unverified"})
			return
		}
		if member.Nickname != *input.ExpectedNickname {
			writeError(w, 409, "nickname_changed_refresh_required")
			return
		}
		route = "/group/set_group_member_info"
		payload = map[string]any{"members": []map[string]string{{
			"groupID": input.TargetID, "userID": member.UserID, "nickname": input.Nickname,
		}}}
	}
	receipt := actionReceipt{
		Digest: digest, OperationID: input.OperationID, Status: "unknown",
		Action: input.Action, Kind: input.Kind, TargetID: input.TargetID, Timestamp: time.Now().Unix(),
	}
	ledger.Receipts[input.OperationID] = receipt
	// Match the token store's fsync/rename/private-permission guarantees.
	durable := func() error {
		data, err := json.Marshal(ledger)
		if err != nil {
			return err
		}
		return privateFile(path, data)
	}
	if durable() != nil {
		writeError(w, 503, "action_ledger_unavailable")
		return
	}
	_, err = postJSON(r.Context(), apiAddr()+route, payload, tokens.IMToken)
	if err == nil {
		receipt.Status = "succeeded"
	} else if strings.HasPrefix(err.Error(), "upstream error code ") ||
		strings.HasPrefix(err.Error(), "upstream status 4") {
		receipt.Status, receipt.Error = "rejected", err.Error()
	} else {
		receipt.Error = "action_result_unknown"
	}
	ledger.Receipts[input.OperationID] = receipt
	if durable() != nil {
		receipt.Status, receipt.Error = "unknown", "action_receipt_store_failed"
	}
	receipt.Digest = ""
	writeJSON(w, 200, receipt)
}

func (s *server) groupState(w http.ResponseWriter, r *http.Request) {
	group := r.URL.Query().Get("group_id")
	if group == "" || len(group) > 128 || strings.TrimSpace(group) != group {
		writeError(w, 400, "invalid_group_id")
		return
	}
	tokens, err := s.tokens.load()
	if err != nil || tokens.IMToken == "" || s.upstream == nil {
		writeError(w, 409, "session_unavailable")
		return
	}
	member, err := selfGroupMember(r.Context(), tokens, group)
	if err != nil {
		writeError(w, 422, "group_membership_unverified")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	sequences, err := s.upstream.sequences(ctx)
	if err != nil {
		writeError(w, 503, "history_unavailable")
		return
	}
	messages := []map[string]any{}
	for _, conversation := range []string{"sg_" + group, "g_" + group} {
		end := sequences.MaxSeqs[conversation]
		if end < 1 {
			continue
		}
		var batch sdkws.PullMessageBySeqsResp
		err = s.upstream.rpc(ctx, 1002, &sdkws.PullMessageBySeqsReq{
			UserID: tokens.UserID, SeqRanges: []*sdkws.SeqRange{{
				ConversationID: conversation, Begin: max(int64(1), end-49), End: end, Num: 50,
			}},
		}, &batch)
		if err != nil {
			writeError(w, 503, "history_unavailable")
			return
		}
		if history := batch.Msgs[conversation]; history != nil {
			for _, message := range history.Msgs {
				if message.SendID != tokens.UserID || message.GroupID != group ||
					(message.SessionType != 2 && message.SessionType != 3) || message.ContentType == 2101 {
					continue
				}
				text, _, err := textualContent(message)
				if err != nil {
					continue
				}
				runes := []rune(text)
				if len(runes) > 160 {
					text = string(runes[:160]) + "..."
				}
				messages = append(messages, map[string]any{
					"message_id": message.ClientMsgID, "text": text, "timestamp": message.SendTime / 1000,
				})
			}
		}
	}
	s.actionMu.Lock()
	var ledger actionLedger
	raw, readErr := os.ReadFile(filepath.Join(filepath.Dir(s.tokens.path), "actions.json"))
	receipts := []actionReceipt{}
	if readErr == nil && json.Unmarshal(raw, &ledger) == nil && ledger.UserID == tokens.UserID {
		for _, receipt := range ledger.Receipts {
			if receipt.Kind == "group" && receipt.TargetID == group {
				receipt.Digest = ""
				receipts = append(receipts, receipt)
			}
		}
	}
	s.actionMu.Unlock()
	sort.Slice(receipts, func(i, j int) bool { return receipts[i].Timestamp > receipts[j].Timestamp })
	if len(receipts) > 20 {
		receipts = receipts[:20]
	}
	result := map[string]any{
		"group_id": group, "nickname": member.Nickname, "messages": messages, "receipts": receipts,
		"role_level": member.RoleLevel,
	}
	if info, err := nativeGroupInfo(ctx, tokens, group); err == nil {
		result["announcement"], result["group_status"] = info.Notification, info.Status
	}
	if r.URL.Query().Get("evidence") == "1" {
		evidence := []map[string]any{}
		for _, conversation := range []string{"n_" + group, "sg_" + group, "g_" + group} {
			end := sequences.MaxSeqs[conversation]
			if end < 1 {
				continue
			}
			var batch sdkws.PullMessageBySeqsResp
			err := s.upstream.rpc(ctx, 1002, &sdkws.PullMessageBySeqsReq{
				UserID: tokens.UserID, SeqRanges: []*sdkws.SeqRange{{
					ConversationID: conversation, Begin: max(int64(1), end-99), End: end, Num: 100,
				}},
			}, &batch)
			if err != nil {
				writeError(w, 503, "history_unavailable")
				return
			}
			messages := batch.NotificationMsgs[conversation]
			if messages == nil {
				messages = batch.Msgs[conversation]
			}
			if messages == nil {
				continue
			}
			for _, message := range messages.Msgs {
				if message.MsgFrom != 200 || message.GroupID != group || message.ContentType < 1500 || message.ContentType > 1599 {
					continue
				}
				var wrapper struct {
					Detail string `json:"detail"`
				}
				var detail map[string]any
				if len(message.Content) > 65536 || json.Unmarshal(message.Content, &wrapper) != nil || json.Unmarshal([]byte(wrapper.Detail), &detail) != nil {
					continue
				}
				evidence = append(evidence, map[string]any{
					"message_id": message.ClientMsgID, "timestamp": message.SendTime / 1000,
					"content_type": message.ContentType, "detail": detail,
					"parsed": notificationMessage(message, tokens.UserID),
				})
			}
		}
		result["protocol_evidence"] = evidence
	}
	writeJSON(w, 200, result)
}

// Querying an unknown receipt is read-only; it must never repeat the mutation.
func (s *server) actionStatus(w http.ResponseWriter, r *http.Request) {
	s.actionMu.Lock()
	defer s.actionMu.Unlock()
	tokens, err := s.tokens.load()
	var ledger actionLedger
	raw, readErr := os.ReadFile(filepath.Join(filepath.Dir(s.tokens.path), "actions.json"))
	if err != nil || readErr != nil || json.Unmarshal(raw, &ledger) != nil || ledger.UserID != tokens.UserID {
		writeError(w, 404, "action_receipt_not_found")
		return
	}
	receipt, ok := ledger.Receipts[r.PathValue("id")]
	if !ok {
		writeError(w, 404, "action_receipt_not_found")
		return
	}
	receipt.Digest = ""
	writeJSON(w, 200, receipt)
}
