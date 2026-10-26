package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"time"

	"github.com/gorilla/websocket"
	"github.com/openimsdk/protocol/sdkws"
)

type cursorFile struct {
	UserID string           `json:"user_id"`
	Seqs   map[string]int64 `json:"sequences"`
}

func saveCursor(path string, c cursorFile) error {
	data, err := json.Marshal(c)
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(path), ".cursor-*")
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
	return os.Rename(f.Name(), path)
}

// Only one consumer owns the account cursor. Each event must be acknowledged
// after AstrBot has queued it; unacknowledged events replay after reconnect.
func (s *server) stream(w http.ResponseWriter, r *http.Request) {
	if s.upstream == nil {
		writeError(w, 503, "upstream_unavailable")
		return
	}
	if !s.consumer.TryLock() {
		writeError(w, 409, "consumer_already_connected")
		return
	}
	defer s.consumer.Unlock()
	conn, err := (&websocket.Upgrader{}).Upgrade(w, r, nil)
	if err != nil {
		return
	}
	defer conn.Close()
	conn.SetReadLimit(4096)
	ctx, cancel := context.WithCancel(r.Context())
	defer cancel()
	acks := make(chan string, 16)
	go func() {
		defer cancel()
		for {
			var event struct {
				Type      string `json:"type"`
				MessageID string `json:"message_id"`
			}
			if conn.ReadJSON(&event) != nil {
				return
			}
			if event.Type == "ack" {
				select {
				case acks <- event.MessageID:
				case <-ctx.Done():
					return
				}
			}
		}
	}()
	write := func(v any) error {
		conn.SetWriteDeadline(time.Now().Add(5 * time.Second))
		return conn.WriteJSON(v)
	}
	tokens, err := s.tokens.load()
	if err != nil || tokens.UserID == "" {
		write(map[string]any{"type": "ready", "data": map[string]any{"status": "logged_out"}})
		return
	}
	path := filepath.Join(filepath.Dir(s.tokens.path), "cursor.json")
	var cursor cursorFile
	raw, err := os.ReadFile(path)
	if err == nil {
		if json.Unmarshal(raw, &cursor) != nil {
			write(map[string]any{"type": "error", "error": "cursor_invalid"})
			return
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return
	}
	for {
		if ctx.Err() != nil {
			return
		}
		current, err := s.upstream.sequences(ctx)
		state, user := s.upstream.status()
		if user != "" && user != tokens.UserID {
			return
		}
		if write(map[string]any{"type": "ready", "data": map[string]any{"status": state, "account_id": tokens.UserID}}) != nil {
			return
		}
		if err == nil {
			// First attachment starts at the current head, never auto-replies to history.
			if cursor.UserID != tokens.UserID || cursor.Seqs == nil {
				cursor = cursorFile{UserID: tokens.UserID, Seqs: current.MaxSeqs}
				if cursor.Seqs == nil {
					cursor.Seqs = map[string]int64{}
				}
				if saveCursor(path, cursor) != nil {
					return
				}
			} else {
				for conversation, max := range current.MaxSeqs {
					begin := cursor.Seqs[conversation] + 1
					if min := current.MinSeqs[conversation]; min > begin {
						begin = min
					}
					if begin > max {
						continue
					}
					end := min(max, begin+49)
					var batch sdkws.PullMessageBySeqsResp
					err = s.upstream.rpc(ctx, 1002, &sdkws.PullMessageBySeqsReq{UserID: tokens.UserID, SeqRanges: []*sdkws.SeqRange{{ConversationID: conversation, Begin: begin, End: end, Num: 50}}}, &batch)
					if err != nil {
						break
					}
					messages := batch.Msgs[conversation]
					if messages == nil {
						messages = batch.NotificationMsgs[conversation]
					}
					if messages != nil {
						sort.Slice(messages.Msgs, func(i, j int) bool { return messages.Msgs[i].Seq < messages.Msgs[j].Seq })
						for _, m := range messages.Msgs {
							if m.Seq < begin || m.Seq > end {
								continue
							}
							event := textMessage(m, tokens.UserID)
							eventType := "message"
							if event == nil {
								event = notificationMessage(m, tokens.UserID)
								eventType = "notification"
							}
							if event == nil {
								event = unsupportedMediaMessage(m, tokens.UserID)
								eventType = "notification"
							}
							if event != nil {
								if write(map[string]any{"type": eventType, "data": event}) != nil {
									return
								}
								timer := time.NewTimer(60 * time.Second)
							waitAck:
								for {
									select {
									case id := <-acks:
										if id == m.ClientMsgID {
											break waitAck
										}
									case <-ctx.Done():
										timer.Stop()
										return
									case <-timer.C:
										return
									}
								}
								timer.Stop()
							}
							cursor.Seqs[conversation] = m.Seq
							if saveCursor(path, cursor) != nil {
								return
							}
						}
					}
					cursor.Seqs[conversation] = end
					if saveCursor(path, cursor) != nil {
						return
					}
				}
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(3 * time.Second):
		}
	}
}
