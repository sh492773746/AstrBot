package main

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/openimsdk/protocol/sdkws"
)

func TestDiagnosticsRedactsMediaAndPreservesSchema(t *testing.T) {
	message := &sdkws.MsgData{
		ClientMsgID: "image", SendID: "member", GroupID: "g", MsgFrom: 100,
		SessionType: 3, ContentType: 102, Content: []byte(`{
			"sourcePath": "/home/private/image.jpg",
			"sourcePicture":{"width":1080,"height":720,"size":32000,"type":"image/jpeg",
			"url":"https://cdn.example.test/private/image.jpg?token=SECRET"}}`),
	}
	row := diagnosticMessage(message, "self", "group", "g")
	raw, _ := json.Marshal(row)
	for _, forbidden := range []string{"SECRET", "/private/", "token=", "/home/"} {
		if strings.Contains(string(raw), forbidden) {
			t.Fatal("sensitive media address leaked")
		}
	}
	for _, expected := range []string{"sourcePicture", "width", "1080", "image/jpeg", "cdn.example.test", "sha256"} {
		if !strings.Contains(string(raw), expected) {
			t.Fatal("schema evidence missing: " + expected)
		}
	}
	if diagnosticMessage(message, "self", "group", "other") != nil ||
		diagnosticMessage(message, "self", "private", "member") != nil {
		t.Fatal("history inspection crossed conversation")
	}
	message.Content = []byte(strings.Repeat("x", 65537))
	if diagnosticMessage(message, "self", "group", "g")["content_error"] != "content_too_large" {
		t.Fatal("inspection must be bounded")
	}
}

func TestDiagnosticPrivateTextScope(t *testing.T) {
	message := &sdkws.MsgData{
		ClientMsgID: "private", SendID: "member", RecvID: "self", MsgFrom: 100,
		SessionType: 1, ContentType: 101, Content: []byte(`{"content":"test reply"}`),
	}
	if diagnosticMessage(message, "self", "private", "member")["text"] != "test reply" {
		t.Fatal("own conversation evidence unavailable")
	}
	if diagnosticMessage(message, "other", "private", "member") != nil {
		t.Fatal("foreign conversation evidence exposed")
	}
	message.MsgFrom = 200
	if diagnosticMessage(message, "self", "private", "member") != nil {
		t.Fatal("system messages exposed as chat messages")
	}
}
