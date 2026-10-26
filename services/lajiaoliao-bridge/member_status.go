package main

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"time"
)

// Availability is based only on explicit authenticated server fields.
// Missing fields stay unknown; display names and avatars are never evidence.
func (s *server) memberAvailability(w http.ResponseWriter, r *http.Request) {
	group, uid := r.URL.Query().Get("group_id"), r.URL.Query().Get("user_id")
	if group == "" || uid == "" || len(group) > 128 || len(uid) > 128 ||
		strings.TrimSpace(group) != group || strings.TrimSpace(uid) != uid {
		writeError(w, 400, "invalid_target_member")
		return
	}
	tokens, err := s.tokens.load()
	if err != nil || tokens.ChatToken == "" {
		writeError(w, 409, "session_unavailable")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	if _, err := selfGroupMember(ctx, tokens, group); err != nil {
		writeError(w, 403, "group_membership_unverified")
		return
	}
	if _, err := verifiedGroupMember(ctx, tokens, group, uid); err != nil {
		writeError(w, 403, "target_membership_unverified")
		return
	}
	raw, err := postJSONWithPlatform(ctx, chatAddr()+"/user/find/full",
		map[string]any{"userIDs": []string{uid}}, tokens.ChatToken, "3", "astrbot-lajiaoliao-bridge")
	var envelope struct {
		Data struct {
			Users []map[string]json.RawMessage `json:"users"`
		} `json:"data"`
	}
	status := "unknown"
	observed := false
	if err == nil && json.Unmarshal(raw, &envelope) == nil && len(envelope.Data.Users) == 1 {
		person := envelope.Data.Users[0]
		var id string
		json.Unmarshal(person["userID"], &id)
		var value int
		if id == uid && len(person["accountStatus"]) != 0 && json.Unmarshal(person["accountStatus"], &value) == nil {
			// The server's enum semantics have not been accepted against a
			// designated banned/deactivated member. Do not guess from a number.
			observed = true
		}
	}
	writeJSON(w, 200, map[string]any{"user_id": uid, "group_id": group, "availability": status, "verified": false, "status_field_observed": observed})
}
