package main

import (
	"encoding/json"
	"strings"

	"github.com/openimsdk/protocol/sdkws"
)

// Notifications are transport metadata, never model input. Decode only known
// SDK shapes and validate the conversation against the outer server envelope.
func notificationMessage(m *sdkws.MsgData, self string) map[string]any {
	if m.MsgFrom != 200 || (m.ContentType != 2101 && m.ContentType != 1516 && m.ContentType != 1519 &&
		m.ContentType != 1502 && m.ContentType != 1508 && m.ContentType != 1509 &&
		m.ContentType != 1510 && (m.ContentType < 1512 || m.ContentType > 1515)) {
		return nil
	}
	var wrapper struct {
		Detail string `json:"detail"`
	}
	if json.Unmarshal(m.Content, &wrapper) != nil {
		return nil
	}
	event := map[string]any{"message_id": m.ClientMsgID, "group_id": m.GroupID, "timestamp": m.SendTime / 1000}
	if m.ContentType == 2101 {
		var detail struct {
			MessageID      string `json:"clientMsgID"`
			RevokerUserID  string `json:"revokerUserID"`
			RevokerID      string `json:"revokerID"`
			ConversationID string `json:"conversationID"`
			SessionType    int32  `json:"sessionType"`
			SesstionType   int32  `json:"sesstionType"`
		}
		if json.Unmarshal([]byte(wrapper.Detail), &detail) != nil || detail.MessageID == "" || len(detail.MessageID) > 128 {
			return nil
		}
		kind, target := "group", m.GroupID
		if target != "" {
			if m.SessionType != 2 && m.SessionType != 3 {
				return nil
			}
			if detail.ConversationID != "" && detail.ConversationID != "sg_"+target && detail.ConversationID != "g_"+target {
				return nil
			}
		} else {
			kind = "private"
			if m.SessionType != 1 {
				return nil
			}
			target = m.SendID
			if target == self {
				target = m.RecvID
			}
			if target == "" || (m.SendID != self && m.RecvID != self) {
				return nil
			}
			a, b := self, target
			if a > b {
				a, b = b, a
			}
			if detail.ConversationID != "" && detail.ConversationID != "si_"+a+"_"+b {
				return nil
			}
		}
		event["action"], event["kind"], event["target_id"] = "message_revoked", kind, target
		event["revoked_message_id"] = detail.MessageID
		event["operator_id"] = detail.RevokerUserID
		if detail.RevokerUserID == "" {
			event["operator_id"] = detail.RevokerID
		}
	} else if m.ContentType == 1516 {
		var detail struct {
			Group struct {
				GroupID string `json:"groupID"`
			} `json:"group"`
			ChangedUser struct {
				GroupID  string `json:"groupID"`
				UserID   string `json:"userID"`
				Nickname string `json:"nickname"`
			} `json:"changedUser"`
		}
		if json.Unmarshal([]byte(wrapper.Detail), &detail) != nil || m.GroupID == "" ||
			detail.Group.GroupID != m.GroupID || detail.ChangedUser.GroupID != m.GroupID ||
			detail.ChangedUser.UserID == "" || len(detail.ChangedUser.Nickname) > 256 ||
			strings.ContainsAny(detail.ChangedUser.Nickname, "\r\n") {
			return nil
		}
		event["action"] = "group_member_updated"
		event["user_id"], event["nickname"] = detail.ChangedUser.UserID, detail.ChangedUser.Nickname
	} else {
		var detail struct {
			Group           nativeGroup   `json:"group"`
			OpUser          groupMember   `json:"opUser"`
			InviterUser     groupMember   `json:"inviterUser"`
			MutedUser       groupMember   `json:"mutedUser"`
			InvitedUserList []groupMember `json:"invitedUserList"`
			KickedUserList  []groupMember `json:"kickedUserList"`
			EntrantUser     struct {
				GroupID       string `json:"groupID"`
				UserID        string `json:"userID"`
				InviterUserID string `json:"inviterUserID"`
			} `json:"entrantUser"`
			MutedSeconds uint32 `json:"mutedSeconds"`
		}
		if json.Unmarshal([]byte(wrapper.Detail), &detail) != nil || m.GroupID == "" ||
			detail.Group.GroupID != m.GroupID || (m.SessionType != 2 && m.SessionType != 3) {
			return nil
		}
		event["operator_id"], event["content_type"] = detail.OpUser.UserID, m.ContentType
		switch m.ContentType {
		case 1502, 1519:
			if len(detail.Group.Notification) > 16384 {
				return nil
			}
			event["action"], event["announcement"], event["group_status"] = "group_info_updated", detail.Group.Notification, detail.Group.Status
			if m.ContentType == 1519 {
				event["action"] = "group_announcement_updated"
			}
		case 1508, 1509:
			members := detail.KickedUserList
			event["action"] = "members_kicked"
			if m.ContentType == 1509 {
				members = detail.InvitedUserList
				event["action"], event["inviter_id"] = "members_invited", detail.InviterUser.UserID
				if detail.InviterUser.UserID == "" {
					event["inviter_id"] = detail.OpUser.UserID
				}
			}
			if len(members) == 0 || len(members) > 100 {
				return nil
			}
			ids := []string{}
			seen := map[string]bool{}
			for _, member := range members {
				if member.UserID == "" || len(member.UserID) > 128 || seen[member.UserID] || (member.GroupID != "" && member.GroupID != m.GroupID) {
					return nil
				}
				seen[member.UserID] = true
				ids = append(ids, member.UserID)
			}
			event["member_ids"] = ids
		case 1510:
			if detail.EntrantUser.UserID == "" || (detail.EntrantUser.GroupID != "" && detail.EntrantUser.GroupID != m.GroupID) {
				return nil
			}
			event["action"], event["member_ids"], event["inviter_id"] = "member_entered", []string{detail.EntrantUser.UserID}, detail.EntrantUser.InviterUserID
		case 1512, 1513:
			if detail.MutedUser.UserID == "" || (detail.MutedUser.GroupID != "" && detail.MutedUser.GroupID != m.GroupID) {
				return nil
			}
			event["action"], event["user_id"], event["muted_seconds"] = "member_muted", detail.MutedUser.UserID, detail.MutedSeconds
			if m.ContentType == 1513 {
				event["action"] = "member_unmuted"
			}
		case 1514, 1515:
			event["action"] = "group_muted"
			if m.ContentType == 1515 {
				event["action"] = "group_unmuted"
			}
		}
	}
	return event
}
