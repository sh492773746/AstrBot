package main

import (
	"context"
	"crypto/md5"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	msgproto "github.com/openimsdk/protocol/msg"
	"github.com/openimsdk/protocol/sdkws"
)

type errorResponse struct {
	Error string `json:"error"`
}

type sendRequest struct {
	Kind     string           `json:"kind"`
	TargetID string           `json:"target_id"`
	ReplyTo  string           `json:"reply_to"`
	Parts    []map[string]any `json:"parts"`
}

type loginRequest struct {
	Account            string `json:"account"`
	Password           string `json:"password"`
	CaptchaVerifyParam string `json:"captcha_verify_param"`
	ExpectedUserID     string `json:"expected_user_id"`
}

type loginResponse struct {
	ErrCode int `json:"errCode"`
	Data    *struct {
		UserID                           string `json:"userID"`
		IMToken                          string `json:"imToken"`
		ChatToken                        string `json:"chatToken"`
		NeedConfirmSameClassDeviceSwitch bool   `json:"needConfirmSameClassDeviceSwitch"`
	} `json:"data"`
	UserID                           string `json:"userID"`
	IMToken                          string `json:"imToken"`
	ChatToken                        string `json:"chatToken"`
	NeedConfirmSameClassDeviceSwitch bool   `json:"needConfirmSameClassDeviceSwitch"`
}

type broker struct {
	mu    sync.Mutex
	peers map[*websocket.Conn]struct{}
}

func (b *broker) add(conn *websocket.Conn) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.peers[conn] = struct{}{}
}

func (b *broker) remove(conn *websocket.Conn) {
	b.mu.Lock()
	defer b.mu.Unlock()
	delete(b.peers, conn)
}

func (b *broker) broadcast(payload any) {
	data, err := json.Marshal(payload)
	if err != nil {
		return
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	for peer := range b.peers {
		if err := peer.WriteMessage(websocket.TextMessage, data); err != nil {
			_ = peer.Close()
			delete(b.peers, peer)
		}
	}
}

type server struct {
	broker     *broker
	tokens     *tokenStore
	upstream   *openIM
	consumer   sync.Mutex
	actionMu   sync.Mutex
	mediaMu    sync.Mutex
	mediaSlots chan struct{}
	images     map[string]imageResource
}

func (s *server) status(w http.ResponseWriter, _ *http.Request) {
	tokens, err := s.tokens.load()
	if err != nil {
		writeError(w, http.StatusInternalServerError, "token_store_unavailable")
		return
	}
	state := "disconnected"
	if s.upstream != nil {
		state, _ = s.upstream.status()
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status":           statusForTokens(tokens),
		"protocol_version": bridgeProtocolVersion,
		"transport":        "openim",
		"authenticated":    tokens.ChatToken != "" && tokens.IMToken != "",
		"api_addr":         apiAddr(),
		"ws_addr":          wsAddr(),
		"account_id":       tokens.UserID,
		"connection_state": state,
		"capabilities":     map[string]bool{"text": true, "image": true, "audio": false, "file": false, "mention": false, "quote": true, "revoke": true},
		"capability_details": map[string]any{
			"receive":      map[string]bool{"text": true, "image": true, "quote": true, "revoke": true, "group_member_updated": true},
			"send":         map[string]bool{"text": true, "mention": false, "quote": true, "image": true, "audio": false, "file": false},
			"actions":      map[string]bool{"revoke_self_message": true, "set_self_group_nickname": true, "revoke_others": true, "rename_others": true, "cleanup": false, "mute": true, "unmute": true, "kick": true, "announce": true, "mute_all": true, "unmute_all": true},
			"verification": map[string]bool{"revoke_others": false, "rename_others": false, "cleanup": false},
			"acceptance":   "see_plugin_evidence_matrix",
		},
		"credential_version": tokens.Version,
	})
}

func (s *server) refreshSession(w http.ResponseWriter, r *http.Request) {
	if s.upstream == nil {
		writeError(w, 503, "upstream_unavailable")
		return
	}
	state, _ := s.upstream.status()
	if state != "token_expired" {
		writeError(w, 409, "token_expiry_not_confirmed")
		return
	}
	tokens, err := s.tokens.load()
	if err != nil {
		writeError(w, 500, "token_store_unavailable")
		return
	}
	s.upstream.mu.Lock()
	expired := s.upstream.token
	s.upstream.mu.Unlock()
	if expired != tokens.IMToken {
		writeError(w, 409, "credential_version_changed")
		return
	}
	result, err := s.tokens.refresh(r.Context(), tokens)
	if err != nil {
		writeError(w, 409, err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"status": "renewed", "credential_version": result.Version})
}

func (s *server) login(w http.ResponseWriter, r *http.Request) {
	var request loginRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10)).Decode(&request); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_json")
		return
	}
	account := strings.TrimSpace(request.Account)
	if account == "" || request.Password == "" {
		writeError(w, http.StatusBadRequest, "account_and_password_required")
		return
	}
	phone := strings.TrimPrefix(account, "+86")
	if len(phone) != 11 || phone[0] != '1' {
		writeError(w, http.StatusBadRequest, "phone_account_required")
		return
	}

	requiresCaptcha, err := passwordLoginRequiresCaptcha(r.Context())
	if err != nil {
		writeError(w, http.StatusBadGateway, "lajiaoliao_verification_config_unavailable")
		return
	}
	if requiresCaptcha && strings.TrimSpace(request.CaptchaVerifyParam) == "" {
		writeError(w, http.StatusPreconditionRequired, "lajiaoliao_captcha_required")
		return
	}

	passwordMD5 := fmt.Sprintf("%x", md5.Sum([]byte(request.Password)))
	payload := map[string]any{
		"areaCode":                     "+86",
		"phoneNumber":                  phone,
		"passwordMD5":                  passwordMD5,
		"verifyCode":                   "",
		"confirmSameClassDeviceSwitch": false,
		"platform":                     3,
		"deviceID":                     "device_windows_astrbot-lajiaoliao-bridge",
		"deviceName":                   "Windows",
	}
	if request.CaptchaVerifyParam != "" {
		payload["captchaVerifyParam"] = request.CaptchaVerifyParam
	}
	response, err := postJSONWithPlatform(
		r.Context(),
		chatAddr()+"/pc/account/login",
		payload,
		"",
		"3",
		"device_windows_astrbot-lajiaoliao-bridge",
	)
	if err != nil {
		writeError(w, http.StatusBadGateway, "lajiaoliao_login_failed")
		return
	}
	var loginResult loginResponse
	if json.Unmarshal(response, &loginResult) == nil && (loginResult.NeedConfirmSameClassDeviceSwitch ||
		(loginResult.Data != nil && loginResult.Data.NeedConfirmSameClassDeviceSwitch)) {
		writeError(w, http.StatusConflict, "lajiaoliao_device_confirmation_required")
		return
	}
	tokens, ok := extractTokens(response)
	if !ok {
		writeJSON(w, http.StatusBadGateway, map[string]any{
			"error":   "lajiaoliao_login_failed",
			"message": "login response did not contain OpenIM tokens",
		})
		return
	}
	if request.ExpectedUserID != "" && request.ExpectedUserID != tokens.UserID {
		writeError(w, http.StatusConflict, "reauth_account_mismatch")
		return
	}
	refreshed, err := postJSONWithPlatform(
		r.Context(),
		chatAddr()+"/pc/account/get_im_token",
		map[string]any{"platform": 5},
		tokens.ChatToken,
		"5",
		"astrbot-lajiaoliao-bridge",
	)
	var refreshResult loginResponse
	if err != nil || json.Unmarshal(refreshed, &refreshResult) != nil || refreshResult.ErrCode != 0 {
		writeError(w, http.StatusBadGateway, "lajiaoliao_im_token_refresh_failed")
		return
	}
	refreshedIMToken := refreshResult.IMToken
	if refreshResult.Data != nil && refreshResult.Data.IMToken != "" {
		refreshedIMToken = refreshResult.Data.IMToken
	}
	if refreshedIMToken == "" {
		writeError(w, http.StatusBadGateway, "lajiaoliao_im_token_refresh_failed")
		return
	}
	tokens.IMToken = refreshedIMToken
	if err := s.tokens.save(tokens); err != nil {
		writeError(w, http.StatusInternalServerError, "token_store_unavailable")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status":     "logged_in",
		"account_id": tokens.UserID,
		"api_addr":   apiAddr(),
		"ws_addr":    wsAddr(),
	})
}

func (s *server) logout(w http.ResponseWriter, _ *http.Request) {
	if err := s.tokens.clear(); err != nil {
		writeError(w, http.StatusInternalServerError, "token_store_unavailable")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "logged_out"})
}

func (s *server) groups(w http.ResponseWriter, r *http.Request) {
	s.directory(w, r, "/group/get_joined_group_list", map[string]any{})
}

func (s *server) members(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "" {
		writeError(w, 400, "group_id_required")
		return
	}
	s.directory(w, r, "/group/get_group_member_list", map[string]any{"groupID": id, "filter": 0})
}

func (s *server) directory(w http.ResponseWriter, r *http.Request, route string, payload map[string]any) {
	tokens, err := s.tokens.load()
	if err != nil || tokens.IMToken == "" {
		writeError(w, 401, "session_unavailable")
		return
	}
	payload["userID"] = tokens.UserID
	page := 1
	if value := r.URL.Query().Get("page"); value != "" {
		if _, err := fmt.Sscanf(value, "%d", &page); err != nil || page < 1 {
			writeError(w, 400, "invalid_page")
			return
		}
	}
	payload["pagination"] = map[string]int{"pageNumber": page, "showNumber": 100}
	data, err := postJSON(r.Context(), apiAddr()+route, payload, tokens.IMToken)
	if err != nil {
		writeError(w, 502, "directory_request_failed")
		return
	}
	var result struct {
		Data json.RawMessage `json:"data"`
	}
	if json.Unmarshal(data, &result) != nil {
		writeError(w, 502, "invalid_directory_response")
		return
	}
	writeJSON(w, 200, result.Data)
}

func (s *server) messages(w http.ResponseWriter, r *http.Request) {
	var request sendRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4<<20)).Decode(&request); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_json")
		return
	}
	if request.TargetID == "" || len(request.Parts) == 0 {
		writeError(w, http.StatusBadRequest, "target_and_parts_required")
		return
	}
	if request.Kind != "private" && request.Kind != "group" {
		writeError(w, 400, "message_kind_required")
		return
	}
	quoteID := request.ReplyTo
	if len(quoteID) > 128 || strings.TrimSpace(quoteID) != quoteID {
		writeError(w, 400, "invalid_quote_id")
		return
	}
	var text strings.Builder
	var imageContent []byte
	for _, part := range request.Parts {
		if part["type"] == "quote" {
			id, ok := part["message_id"].(string)
			if !ok || id == "" || len(id) > 128 || strings.TrimSpace(id) != id {
				writeError(w, 400, "invalid_quote_id")
				return
			}
			if quoteID != "" {
				writeError(w, 400, "multiple_quotes")
				return
			}
			quoteID = id
			continue
		}
		if part["type"] == "image" {
			ref, ok := part["image_ref"].(string)
			if !ok || ref == "" || imageContent != nil {
				writeError(w, 422, "single_image_ref_required")
				return
			}
			var err error
			imageContent, err = s.imageContent(ref)
			if err != nil {
				writeError(w, 422, err.Error())
				return
			}
			continue
		}
		if part["type"] != "text" {
			writeError(w, 422, "unsupported_message_component")
			return
		}
		value, ok := part["text"].(string)
		if !ok {
			writeError(w, 400, "invalid_text")
			return
		}
		text.WriteString(value)
	}
	if imageContent != nil && (text.Len() != 0 || quoteID != "") {
		writeError(w, 422, "mixed_image_requires_ordered_batch")
		return
	}
	if (text.Len() == 0 && imageContent == nil) || text.Len() > 16384 {
		writeError(w, 400, "invalid_text_length")
		return
	}
	if s.upstream == nil {
		writeError(w, 503, "upstream_unavailable")
		return
	}
	tokens, err := s.tokens.load()
	if err != nil || tokens.UserID == "" {
		writeError(w, 401, "session_unavailable")
		return
	}
	content, _ := json.Marshal(map[string]string{"content": text.String()})
	data := &sdkws.MsgData{SendID: tokens.UserID, RecvID: request.TargetID, ClientMsgID: newID(), SenderPlatformID: 5, SessionType: 1, MsgFrom: 100, ContentType: 101, Content: content, CreateTime: time.Now().UnixMilli(), Options: map[string]bool{"history": true, "persistent": true, "senderSync": true}}
	if imageContent != nil {
		data.ContentType, data.Content = 102, imageContent
	}
	if request.Kind == "group" {
		data.GroupID = request.TargetID
		data.SessionType = 3
	}
	if quoteID != "" {
		original, err := s.resolveQuote(r.Context(), tokens.UserID, request.Kind, request.TargetID, quoteID)
		if err != nil {
			code := 422
			if err.Error() == "quote_lookup_failed" {
				code = 502
			}
			writeJSON(w, code, map[string]any{"error": err.Error(), "status": "rejected"})
			return
		}
		content, err := quoteEnvelope(text.String(), original)
		if err != nil {
			writeJSON(w, 422, map[string]any{"error": err.Error(), "status": "rejected"})
			return
		}
		data.ContentType, data.Content = 114, content
	}
	var result msgproto.SendMsgResp
	if err := s.upstream.rpc(r.Context(), 1003, data, &result); err != nil {
		// A lost receipt is not evidence of rejection. Never retry automatically.
		writeJSON(w, 502, map[string]any{"error": "message_send_result_unknown", "status": "unknown", "message_id": data.ClientMsgID})
		return
	}
	if result.ServerMsgID == "" || result.ClientMsgID != data.ClientMsgID {
		writeJSON(w, 502, map[string]any{"error": "invalid_send_receipt", "status": "unknown", "message_id": data.ClientMsgID})
		return
	}
	writeJSON(w, 200, map[string]any{"status": "sent", "message_id": result.ClientMsgID, "server_message_id": result.ServerMsgID, "timestamp": result.SendTime})
}

func (s *server) events(w http.ResponseWriter, r *http.Request) {
	upgrader := websocket.Upgrader{
		CheckOrigin: func(request *http.Request) bool {
			origin := request.Header.Get("Origin")
			if origin == "" {
				return true
			}
			parsed, err := url.Parse(origin)
			if err != nil {
				return false
			}
			host := strings.ToLower(parsed.Hostname())
			return host == "127.0.0.1" || host == "localhost" || host == "::1"
		},
	}
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		return
	}
	s.broker.add(conn)
	defer s.broker.remove(conn)
	defer conn.Close()
	_ = conn.WriteJSON(map[string]any{
		"type": "ready",
		"data": map[string]any{"status": "upstream_unavailable"},
	})
	for {
		if _, _, err := conn.ReadMessage(); err != nil {
			return
		}
	}
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, errorResponse{Error: message})
}

func apiAddr() string {
	if value := strings.TrimRight(os.Getenv("LAJIAOLIAO_API_ADDR"), "/"); value != "" {
		return value
	}
	return "https://im-api.lajiaoliao.com"
}

// Account routes use CHAT_URL, not the token-protected OpenIM API_URL.
func chatAddr() string {
	if value := strings.TrimRight(os.Getenv("LAJIAOLIAO_CHAT_ADDR"), "/"); value != "" {
		return value
	}
	return "https://api.lajiaoliao.com"
}

func wsAddr() string {
	if value := strings.TrimRight(os.Getenv("LAJIAOLIAO_WS_ADDR"), "/"); value != "" {
		return value
	}
	return "wss://ws.lajiaoliao.com"
}

func statusForTokens(tokens tokenFile) string {
	if tokens.ChatToken != "" && tokens.IMToken != "" {
		return "authenticated"
	}
	return "logged_out"
}

func postJSON(ctx context.Context, endpoint string, payload any, token string) ([]byte, error) {
	return postJSONWithPlatform(ctx, endpoint, payload, token, "5", "astrbot-lajiaoliao-bridge")
}

func postJSONWithPlatform(ctx context.Context, endpoint string, payload any, token, platform, deviceID string) ([]byte, error) {
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, strings.NewReader(string(body)))
	if err != nil {
		return nil, err
	}
	request.Header.Set("Accept", "application/json")
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("operationID", "astrbot-lajiaoliao-bridge")
	request.Header.Set("version", "1.0.3")
	request.Header.Set("platform", platform)
	request.Header.Set("deviceID", deviceID)
	if token != "" {
		request.Header.Set("token", token)
	}
	response, err := (&http.Client{
		Timeout: 20 * time.Second,
		// These fixed endpoints carry credentials in custom headers and request bodies.
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}).Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	const maxResponseBytes = 2 << 20
	data, err := io.ReadAll(io.LimitReader(response.Body, maxResponseBytes+1))
	if err != nil {
		return nil, err
	}
	if len(data) > maxResponseBytes {
		return nil, fmt.Errorf("upstream response too large")
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, fmt.Errorf("upstream status %d", response.StatusCode)
	}
	var envelope struct {
		ErrCode *int `json:"errCode"`
	}
	if err := json.Unmarshal(data, &envelope); err != nil || envelope.ErrCode == nil {
		return nil, fmt.Errorf("invalid upstream response")
	}
	if *envelope.ErrCode != 0 {
		return nil, fmt.Errorf("upstream error code %d", *envelope.ErrCode)
	}
	return data, nil
}

func extractTokens(data []byte) (tokenFile, bool) {
	var response loginResponse
	if err := json.Unmarshal(data, &response); err != nil || response.ErrCode != 0 {
		return tokenFile{}, false
	}
	value := response.Data
	tokens := tokenFile{
		UserID:    response.UserID,
		IMToken:   response.IMToken,
		ChatToken: response.ChatToken,
	}
	if value != nil {
		if value.UserID != "" {
			tokens.UserID = value.UserID
		}
		if value.IMToken != "" {
			tokens.IMToken = value.IMToken
		}
		if value.ChatToken != "" {
			tokens.ChatToken = value.ChatToken
		}
	}
	return tokens, tokens.UserID != "" && tokens.ChatToken != "" && tokens.IMToken != ""
}

func main() {
	if len(os.Args) == 2 && os.Args[1] == "--managed" {
		if err := runManaged(); err != nil {
			log.Fatal(err)
		}
		return
	}
	addr := os.Getenv("LAJIAOLIAO_BRIDGE_ADDR")
	if addr == "" {
		addr = "127.0.0.1:17890"
	}
	if !strings.HasPrefix(addr, "127.0.0.1:") {
		log.Fatalf("bridge address must be loopback: %s", addr)
	}
	tokenPath := os.Getenv("LAJIAOLIAO_BRIDGE_TOKEN_FILE")
	if tokenPath == "" {
		tokenPath = filepath.Join("data", "lajiaoliao-bridge", "token.json")
	}
	s := &server{
		broker: &broker{peers: make(map[*websocket.Conn]struct{})},
		tokens: newTokenStore(tokenPath),
	}
	s.upstream = &openIM{store: s.tokens}
	go s.upstream.run(context.Background())
	httpServer := &http.Server{Addr: addr, Handler: s.routes(), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: 75 * time.Second, IdleTimeout: 60 * time.Second}
	log.Printf("lajiaoliao bridge listening on %s", addr)
	if err := httpServer.ListenAndServe(); err != nil {
		log.Fatal(err)
	}
}
