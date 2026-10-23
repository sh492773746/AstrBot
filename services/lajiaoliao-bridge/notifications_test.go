package main

import (
	"encoding/json"
	"testing"

	"github.com/openimsdk/protocol/sdkws"
)

func TestVerifiedNotificationShapes(t *testing.T) {
	wrap := func(detail any) []byte {
		data, _ := json.Marshal(detail)
		result, _ := json.Marshal(map[string]string{"detail": string(data)})
		return result
	}
	m := &sdkws.MsgData{
		ClientMsgID: "notification", SendID: "owner", RecvID: "group", GroupID: "group",
		SessionType: 3, MsgFrom: 200, ContentType: 2101,
		Content: wrap(map[string]any{"clientMsgID": "original", "revokerUserID": "owner", "conversationID": "sg_group", "sesstionType": 3}),
	}
	event := notificationMessage(m, "bot")
	if event["revoked_message_id"] != "original" || event["target_id"] != "group" ||
		event["operator_id"] != "owner" || event["action"] != "message_revoked" {
		t.Fatal("recall metadata lost")
	}
	m.MsgFrom = 100
	if notificationMessage(m, "bot") != nil {
		t.Fatal("non-system notification accepted")
	}
	m.MsgFrom, m.GroupID = 200, "another"
	if notificationMessage(m, "bot") != nil {
		t.Fatal("cross-conversation revoke accepted")
	}
	m.GroupID, m.ContentType = "group", 1516
	m.Content = wrap(map[string]any{
		"group":       map[string]string{"groupID": "group"},
		"changedUser": map[string]string{"groupID": "group", "userID": "bot", "nickname": "new card"},
	})
	event = notificationMessage(m, "bot")
	if event["nickname"] != "new card" || event["user_id"] != "bot" {
		t.Fatal("group card metadata lost")
	}
	m.GroupID = "other"
	if notificationMessage(m, "bot") != nil {
		t.Fatal("cross-group card accepted")
	}
	m.Content = []byte(`{"detail":"bad-json"}`)
	if notificationMessage(m, "bot") != nil {
		t.Fatal("malformed notification accepted")
	}
}

func TestNativeNotificationIdentityAndKnownShapes(t *testing.T) {
	for _, tc := range []struct {
		code          int32
		action, field string
	}{
		{1502, "group_info_updated", ""},
		{1519, "group_announcement_updated", ""},
		{1508, "members_kicked", "kickedUserList"},
		{1509, "members_invited", "invitedUserList"},
		{1510, "member_entered", "entrantUser"},
		{1512, "member_muted", "mutedUser"},
		{1513, "member_unmuted", "mutedUser"},
		{1514, "group_muted", ""},
		{1515, "group_unmuted", ""},
	} {
		detail := map[string]any{
			"group":       map[string]any{"groupID": "g", "notification": "edited", "status": 0},
			"opUser":      map[string]string{"userID": "owner"},
			"inviterUser": map[string]string{"userID": "inviter"}, "mutedSeconds": 600,
		}
		member := map[string]string{"groupID": "g", "userID": "person", "inviterUserID": "inviter"}
		if tc.field == "kickedUserList" || tc.field == "invitedUserList" {
			detail[tc.field] = []any{member}
		} else if tc.field != "" {
			detail[tc.field] = member
		}
		raw, _ := json.Marshal(detail)
		wrapped, _ := json.Marshal(map[string]string{"detail": string(raw)})
		message := &sdkws.MsgData{ClientMsgID: "n", MsgFrom: 200, SessionType: 3, GroupID: "g", ContentType: tc.code, Content: wrapped}
		got := notificationMessage(message, "bot")
		if got == nil || got["action"] != tc.action {
			t.Fatalf("missing %d notification", tc.code)
		}
		message.GroupID = "other"
		if notificationMessage(message, "bot") != nil {
			t.Fatal("cross-group identity accepted")
		}
		message.GroupID, message.MsgFrom = "g", 100
		if notificationMessage(message, "bot") != nil {
			t.Fatal("user-forged system action accepted")
		}
	}
}
