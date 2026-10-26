package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
)

type captchaScene struct {
	UsedFor int    `json:"usedFor"`
	Enabled bool   `json:"enabled"`
	SceneID string `json:"sceneId"`
}

type captchaConfig struct {
	Enabled  bool           `json:"enabled"`
	Provider string         `json:"provider"`
	Region   string         `json:"region"`
	Prefix   string         `json:"prefix"`
	Scenes   []captchaScene `json:"scenes"`
}

// Only a user-completed official challenge may supply captchaVerifyParam.
// Never send credentials when verification configuration cannot be checked.
func passwordLoginRequiresCaptcha(ctx context.Context) (bool, error) {
	captcha, err := loadCaptchaConfig(ctx)
	if err != nil {
		return false, err
	}
	return captcha.passwordSceneRequired()
}

func loadCaptchaConfig(ctx context.Context) (*captchaConfig, error) {
	data, err := postJSONWithPlatform(ctx, chatAddr()+"/client_config/get",
		map[string]any{}, "", "3", "device_windows_astrbot-lajiaoliao-bridge")
	if err != nil {
		return nil, err
	}
	var response struct {
		Data struct {
			Config *struct {
				Captcha *captchaConfig `json:"captcha"`
			} `json:"config"`
		} `json:"data"`
	}
	if err := json.Unmarshal(data, &response); err != nil {
		return nil, err
	}
	if response.Data.Config == nil || response.Data.Config.Captcha == nil {
		return nil, fmt.Errorf("missing verification configuration")
	}
	return response.Data.Config.Captcha, nil
}

func (captcha *captchaConfig) passwordSceneRequired() (bool, error) {
	if !captcha.Enabled {
		return false, nil
	}
	for _, scene := range captcha.Scenes {
		if scene.UsedFor == 3 {
			return scene.Enabled, nil
		}
	}
	return false, fmt.Errorf("missing password verification scene")
}

func (s *server) challenge(w http.ResponseWriter, r *http.Request) {
	config, err := loadCaptchaConfig(r.Context())
	if err != nil {
		writeError(w, http.StatusBadGateway, "lajiaoliao_verification_config_unavailable")
		return
	}
	required, err := config.passwordSceneRequired()
	if err != nil {
		writeError(w, http.StatusBadGateway, "lajiaoliao_verification_config_unavailable")
		return
	}
	if !required {
		writeJSON(w, http.StatusOK, map[string]any{"required": false})
		return
	}
	for _, scene := range config.Scenes {
		if scene.UsedFor != 3 {
			continue
		}
		if config.Provider != "aliyun" || config.Region == "" || config.Prefix == "" || scene.SceneID == "" {
			writeError(w, http.StatusServiceUnavailable, "lajiaoliao_verification_provider_unsupported")
			return
		}
		// This is public configuration only, never a token or a solved challenge.
		writeJSON(w, http.StatusOK, map[string]any{"required": true, "region": config.Region, "prefix": config.Prefix, "scene_id": scene.SceneID})
		return
	}
}
