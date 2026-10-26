package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"image"
	"image/color"
	"image/gif"
	"image/png"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"github.com/openimsdk/protocol/sdkws"
)

func TestImageValidationAndAddress(t *testing.T) {
	var data bytes.Buffer
	png.Encode(&data, image.NewRGBA(image.Rect(0, 0, 4, 3)))
	config, mime, err := validateImage(data.Bytes())
	if err != nil || config.Width != 4 || mime != "image/png" {
		t.Fatal("PNG validation failed")
	}
	var animated bytes.Buffer
	palette := color.Palette{color.Black, color.White}
	first := image.NewPaletted(image.Rect(0, 0, 4, 3), palette)
	second := image.NewPaletted(image.Rect(0, 0, 4, 3), palette)
	second.SetColorIndex(1, 1, 1)
	if err := gif.EncodeAll(&animated, &gif.GIF{
		Image: []*image.Paletted{first, second},
		Delay: []int{5, 5}, Config: image.Config{Width: 4, Height: 3, ColorModel: palette},
	}); err != nil {
		t.Fatal("GIF fixture creation failed", err)
	}
	config, mime, err = validateImage(animated.Bytes())
	if err != nil || config.Width != 4 || mime != "image/gif" {
		t.Fatal("GIF validation failed")
	}
	if frames, err := gifFrameCount(animated.Bytes()); err != nil || frames != 2 {
		t.Fatal("GIF frame count failed", frames, err)
	}
	for _, data := range [][]byte{nil, []byte("not image"), make([]byte, maxImageBytes+1)} {
		if _, _, err := validateImage(data); err == nil {
			t.Fatal("invalid image accepted")
		}
	}
	tooMany := &gif.GIF{
		Delay:  make([]int, maxGIFFrames+1),
		Config: image.Config{Width: 1, Height: 1, ColorModel: palette},
	}
	for range tooMany.Delay {
		tooMany.Image = append(tooMany.Image, image.NewPaletted(image.Rect(0, 0, 1, 1), palette))
	}
	var tooManyBytes bytes.Buffer
	if err := gif.EncodeAll(&tooManyBytes, tooMany); err != nil {
		t.Fatal("GIF frame-limit fixture creation failed", err)
	}
	if _, _, err := validateImage(tooManyBytes.Bytes()); err == nil {
		t.Fatal("GIF over frame limit accepted")
	}
	for _, location := range []string{
		"http://lj-im-chat-01.lajiaoliao.com/a", "https://127.0.0.1/a",
		"https://lj-im-chat-01.lajiaoliao.com.evil.test/a",
		"https://user@lj-im-chat-01.lajiaoliao.com/a",
		"https://lj-im-chat-01.lajiaoliao.com:8443/a",
	} {
		if imageURL(location) {
			t.Fatal("unsafe location accepted")
		}
	}
}

func TestAuthorizedLiveImageUpload(t *testing.T) {
	path := os.Getenv("LAJIAOLIAO_LIVE_TOKEN_FILE")
	if path == "" {
		t.Skip("explicit local acceptance required")
	}
	tokens, err := newTokenStore(path).load()
	if err != nil {
		t.Fatal("private token store unavailable")
	}
	var data bytes.Buffer
	png.Encode(&data, image.NewRGBA(image.Rect(0, 0, 192, 96)))
	location, err := uploadImage(context.Background(), tokens, newID(), "image/png", data.Bytes())
	if err != nil {
		var rejected *imageLocationError
		if errors.As(err, &rejected) {
			diagnostic, _ := json.Marshal(rejected)
			t.Log(string(diagnostic))
		}
		t.Fatal(imageUploadError(err))
	}
	if !imageURL(location) {
		t.Fatal("unverified image storage")
	}
	client := imageClient()
	defer client.CloseIdleConnections()
	response, err := client.Get(location)
	if err != nil {
		var rejected *imageLocationError
		if errors.As(err, &rejected) {
			diagnostic, _ := json.Marshal(rejected)
			t.Log(string(diagnostic))
		}
		t.Fatal("authorized image download rejected or failed")
	}
	defer response.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(response.Body, maxImageBytes+1))
	if response.StatusCode != 200 || err != nil {
		t.Fatal("authorized image download failed", response.StatusCode)
	}
	if _, _, err := validateImage(raw); err != nil {
		t.Fatal("authorized image bytes invalid")
	}
	t.Log("authorized image storage verified; no chat message sent")
}

func TestDeduplicatedNativeImageUpload(t *testing.T) {
	calls := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("token") != "synthetic-token" {
			t.Error("missing account credential")
		}
		switch r.URL.Path {
		case "/object/part_limit":
			writeJSON(w, 200, map[string]any{"errCode": 0, "data": map[string]int{
				"minPartSize": 1048576, "maxPartSize": 1048576, "maxNumSize": 1,
			}})
		case "/object/initiate_multipart_upload":
			var body map[string]any
			if json.NewDecoder(r.Body).Decode(&body) != nil || body["contentType"] != "image/png" || body["cause"] != "image" {
				t.Error("unexpected native upload contract")
			}
			calls++
			writeJSON(w, 200, map[string]any{"errCode": 0, "data": map[string]string{
				"url": "https://lj-chat-im-01.oss-accelerate.aliyuncs.com/verified-image",
			}})
		default:
			t.Error("unexpected upstream route")
		}
	}))
	defer upstream.Close()
	t.Setenv("LAJIAOLIAO_API_ADDR", upstream.URL)
	var data bytes.Buffer
	png.Encode(&data, image.NewRGBA(image.Rect(0, 0, 4, 3)))
	location, err := uploadImage(context.Background(), tokenFile{UserID: "test", IMToken: "synthetic-token"}, "id", "image/png", data.Bytes())
	if err != nil || !imageURL(location) || calls != 1 {
		t.Fatal("native image upload failed", err)
	}
}

func TestIncomingImageAndAccountScopedReferences(t *testing.T) {
	info := pictureInfo{"id", "image/jpeg", 100, 4, 3, "https://lj-im-chat-01.lajiaoliao.com/image"}
	content, _ := json.Marshal(pictureContent{Source: info, Big: info, Snapshot: info})
	message := &sdkws.MsgData{SendID: "other", RecvID: "self", ClientMsgID: "message",
		ContentType: 102, SessionType: 1, Content: content, SendTime: time.Now().UnixMilli()}
	if event := textMessage(message, "self"); event == nil || event["text"] != "" {
		t.Fatal("incoming image not converted")
	}
	info.URL = "https://127.0.0.1/image"
	message.Content, _ = json.Marshal(pictureContent{Source: info})
	if textMessage(message, "self") != nil {
		t.Fatal("unsafe image not rejected")
	}
	message.MsgFrom, message.ContentType = 100, 103
	if note := unsupportedMediaMessage(message, "self"); note == nil || note["status"] != "unsupported" {
		t.Fatal("unsupported media not explicitly recorded")
	}
	message.SendID = "self"
	if unsupportedMediaMessage(message, "self") != nil {
		t.Fatal("own unsupported media incorrectly recorded")
	}
	one := &server{images: map[string]imageResource{"reference": {Expires: time.Now().Add(time.Minute)}}}
	two := &server{}
	if _, err := one.imageContent("reference"); err != nil {
		t.Fatal(err)
	}
	if _, err := two.imageContent("reference"); err == nil {
		t.Fatal("cross-account reference accepted")
	}
	one.images["reference"] = imageResource{Expires: time.Now().Add(-time.Minute)}
	if _, err := one.imageContent("reference"); err == nil {
		t.Fatal("expired reference accepted")
	}
}

func TestIncomingGIFMetadataIsConvertible(t *testing.T) {
	var raw bytes.Buffer
	frame := image.NewPaletted(image.Rect(0, 0, 8, 6), color.Palette{
		color.Black, color.White,
	})
	frame.SetColorIndex(3, 2, 1)
	if err := gif.Encode(&raw, frame, nil); err != nil {
		t.Fatal(err)
	}
	info := pictureInfo{
		Type: "gif", Size: raw.Len(), Width: 8, Height: 6,
		URL: "https://lj-im-chat-01.lajiaoliao.com/image",
	}
	content, _ := json.Marshal(pictureContent{
		Source: info, Big: info, Snapshot: info,
	})
	message := &sdkws.MsgData{
		SendID: "other", RecvID: "self", ClientMsgID: "gif-message",
		ContentType: 102, SessionType: 3, GroupID: "group",
		Content: content, SendTime: time.Now().UnixMilli(),
	}
	event := textMessage(message, "self")
	if event == nil || event["text"] != "" {
		t.Fatal("GIF image metadata was not accepted")
	}
	components, ok := event["components"].([]map[string]any)
	if !ok || len(components) != 1 || components[0]["type"] != "image" {
		t.Fatal("GIF did not become an image component")
	}
	info.Type = "webp"
	content, _ = json.Marshal(pictureContent{Source: info})
	message.Content = content
	if textMessage(message, "self") != nil {
		t.Fatal("unrequested image format accepted")
	}
}

func TestImageQuoteIsExplicitlyUnsupported(t *testing.T) {
	message := &sdkws.MsgData{
		SendID: "other", RecvID: "self", ClientMsgID: "new",
		ContentType: 114, SessionType: 1, MsgFrom: 100,
		SendTime: time.Now().UnixMilli(),
	}
	message.Content, _ = json.Marshal(quoteContent{
		Text: "Image quote",
		QuoteMessage: &quotedMessage{
			ClientMsgID: "original", SendID: "self", RecvID: "other",
			SessionType: 1, ContentType: 102,
		},
	})
	if textMessage(message, "self") != nil {
		t.Fatal("image quote silently downgraded")
	}
	if note := unsupportedMediaMessage(message, "self"); note == nil || note["status"] != "unsupported" {
		t.Fatal("image quote rejection not recorded")
	}
	message.RecvID = "another"
	if unsupportedMediaMessage(message, "self") != nil {
		t.Fatal("cross-account image quote recorded")
	}
}
