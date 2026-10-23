package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

type tokenStore struct {
	mu        sync.Mutex
	refreshMu sync.Mutex
	path      string
}

type tokenFile struct {
	UserID    string `json:"user_id"`
	IMToken   string `json:"im_token"`
	ChatToken string `json:"chat_token"`
	Version   uint64 `json:"version,omitempty"`
}

func newTokenStore(path string) *tokenStore {
	return &tokenStore{path: path}
}

func (s *tokenStore) load() (tokenFile, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	data, err := os.ReadFile(s.path)
	if errors.Is(err, os.ErrNotExist) {
		return tokenFile{}, nil
	}
	if err != nil {
		return tokenFile{}, err
	}
	var value tokenFile
	if err := json.Unmarshal(data, &value); err != nil {
		return tokenFile{}, err
	}
	return value, nil
}

func (s *tokenStore) save(value tokenFile) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.saveLocked(value)
}

func (s *tokenStore) saveLocked(value tokenFile) error {
	if err := os.MkdirAll(filepath.Dir(s.path), 0o700); err != nil {
		return err
	}
	if err := os.Chmod(filepath.Dir(s.path), 0o700); err != nil {
		return err
	}
	data, err := json.Marshal(value)
	if err != nil {
		return err
	}
	temp, err := os.CreateTemp(filepath.Dir(s.path), ".session-*")
	if err != nil {
		return err
	}
	defer os.Remove(temp.Name())
	if _, err := temp.Write(data); err != nil {
		_ = temp.Close()
		return err
	}
	if err := temp.Sync(); err != nil {
		_ = temp.Close()
		return err
	}
	if err := temp.Close(); err != nil {
		return err
	}
	if err := os.Rename(temp.Name(), s.path); err != nil {
		return err
	}
	dir, err := os.Open(filepath.Dir(s.path))
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

// Refresh only an expired IM token. Never authenticate using a stored password.
func (s *tokenStore) refresh(ctx context.Context, expected tokenFile) (tokenFile, error) {
	s.refreshMu.Lock()
	defer s.refreshMu.Unlock()
	current, err := s.load()
	if err != nil || current.ChatToken == "" || current.UserID == "" {
		return tokenFile{}, errors.New("reauth_required")
	}
	if current != expected {
		return current, nil
	}
	raw, err := postJSONWithPlatform(ctx, chatAddr()+"/pc/account/get_im_token",
		map[string]any{"platform": 5}, current.ChatToken, "5", "astrbot-lajiaoliao-bridge")
	if err != nil {
		if strings.Contains(err.Error(), "upstream status 401") || strings.Contains(err.Error(), "upstream status 403") {
			return tokenFile{}, errors.New("reauth_required")
		}
		for code := 1501; code <= 1507; code++ {
			if err.Error() == fmt.Sprintf("upstream error code %d", code) {
				return tokenFile{}, errors.New("reauth_required")
			}
		}
		return tokenFile{}, errors.New("session_refresh_unavailable")
	}
	var response loginResponse
	if json.Unmarshal(raw, &response) != nil {
		return tokenFile{}, errors.New("session_refresh_invalid")
	}
	if response.ErrCode != 0 {
		if response.ErrCode >= 1501 && response.ErrCode <= 1507 {
			return tokenFile{}, errors.New("reauth_required")
		}
		return tokenFile{}, errors.New("session_refresh_rejected")
	}
	fresh := response.IMToken
	if response.Data != nil && response.Data.IMToken != "" {
		fresh = response.Data.IMToken
	}
	user := response.UserID
	if response.Data != nil && response.Data.UserID != "" {
		user = response.Data.UserID
	}
	if fresh == "" || fresh == current.IMToken || (user != "" && user != current.UserID) {
		return tokenFile{}, errors.New("session_refresh_invalid")
	}
	// A different authorization may have completed during the upstream request.
	s.mu.Lock()
	defer s.mu.Unlock()
	data, err := os.ReadFile(s.path)
	var latest tokenFile
	if err != nil || json.Unmarshal(data, &latest) != nil || latest != expected {
		return tokenFile{}, errors.New("credential_version_changed")
	}
	current.IMToken, current.Version = fresh, current.Version+1
	if err := s.saveLocked(current); err != nil {
		return tokenFile{}, err
	}
	return current, nil
}

func (s *tokenStore) clear() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := os.Remove(s.path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}
