package main

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"unicode"
	"unicode/utf8"
)

func nativeAction(action string) bool {
	switch action {
	case "mute", "unmute", "kick", "announce", "mute_all", "unmute_all":
		return true
	}
	return false
}

func validateNativeAction(input actionRequest) error {
	if input.Kind != "group" || input.MessageID != "" || input.Nickname != "" || input.ExpectedNickname != nil {
		return errors.New("invalid_native_action")
	}
	memberAction := input.Action == "mute" || input.Action == "unmute" || input.Action == "kick"
	if memberAction != (input.UserID != "") || len(input.UserID) > 128 ||
		strings.TrimSpace(input.UserID) != input.UserID ||
		(input.Action == "mute" && (input.MutedSeconds < 60 || input.MutedSeconds > 86400 || input.MutedSeconds%60 != 0)) ||
		(input.Action != "mute" && input.MutedSeconds != 0) ||
		(input.Action == "kick" && !input.ConfirmKick) || (input.Action != "kick" && input.ConfirmKick) {
		return errors.New("invalid_native_action")
	}
	if input.Action == "announce" {
		if input.ExpectedAnnouncement == nil || utf8.RuneCountInString(input.Announcement) > 2000 ||
			len(*input.ExpectedAnnouncement) > 16384 ||
			strings.IndexFunc(input.Announcement, func(r rune) bool { return unicode.IsControl(r) && r != '\n' && r != '\t' }) >= 0 {
			return errors.New("invalid_announcement")
		}
	} else if input.Announcement != "" || input.ExpectedAnnouncement != nil {
		return errors.New("unexpected_action_fields")
	}
	return nil
}

// Native identity is fetched immediately before a fixed mutation. No privilege
// is inferred from a nickname, invitation operator, or client-supplied role.
func nativeActionPayload(ctx context.Context, tokens tokenFile, input actionRequest) (string, any, error) {
	self, err := selfGroupMember(ctx, tokens, input.TargetID)
	if err != nil || (self.RoleLevel != 60 && self.RoleLevel != 100) {
		return "", nil, errors.New("native_admin_required")
	}
	if input.UserID != "" {
		if input.UserID == tokens.UserID {
			return "", nil, errors.New("protected_member")
		}
		raw, err := postJSON(ctx, apiAddr()+"/group/get_group_members_info",
			map[string]any{"groupID": input.TargetID, "userIDs": []string{input.UserID}}, tokens.IMToken)
		var response struct {
			Data struct {
				Members []groupMember `json:"members"`
			} `json:"data"`
		}
		if err != nil || json.Unmarshal(raw, &response) != nil || len(response.Data.Members) != 1 ||
			response.Data.Members[0].UserID != input.UserID || response.Data.Members[0].GroupID != input.TargetID {
			return "", nil, errors.New("target_membership_unverified")
		}
		if response.Data.Members[0].RoleLevel != 20 {
			return "", nil, errors.New("protected_member")
		}
	}
	base := map[string]any{"groupID": input.TargetID}
	switch input.Action {
	case "mute":
		base["userID"], base["mutedSeconds"] = input.UserID, input.MutedSeconds
		return "/group/mute_group_member", base, nil
	case "unmute":
		base["userID"] = input.UserID
		return "/group/cancel_mute_group_member", base, nil
	case "kick":
		base["kickedUserIDs"], base["reason"] = []string{input.UserID}, "Confirmed administrator action"
		return "/group/kick_group", base, nil
	case "mute_all":
		return "/group/mute_group", base, nil
	case "unmute_all":
		return "/group/cancel_mute_group", base, nil
	case "announce":
		info, err := nativeGroupInfo(ctx, tokens, input.TargetID)
		if err != nil {
			return "", nil, err
		}
		if info.Notification != *input.ExpectedAnnouncement {
			return "", nil, errors.New("announcement_changed_refresh_required")
		}
		base["notification"] = input.Announcement
		return "/group/set_group_info", map[string]any{"groupInfoForSet": base}, nil
	}
	return "", nil, errors.New("unsupported_action")
}

type nativeGroup struct {
	GroupID      string `json:"groupID"`
	Notification string `json:"notification"`
	Status       int32  `json:"status"`
	OwnerUserID  string `json:"ownerUserID"`
}

func nativeGroupInfo(ctx context.Context, tokens tokenFile, group string) (nativeGroup, error) {
	raw, err := postJSON(ctx, apiAddr()+"/group/get_groups_info",
		map[string]any{"groupIDs": []string{group}}, tokens.IMToken)
	var response struct {
		Data struct {
			Groups []nativeGroup `json:"groupInfos"`
		} `json:"data"`
	}
	if err != nil || json.Unmarshal(raw, &response) != nil || len(response.Data.Groups) != 1 || response.Data.Groups[0].GroupID != group {
		return nativeGroup{}, errors.New("group_info_unverified")
	}
	return response.Data.Groups[0], nil
}
