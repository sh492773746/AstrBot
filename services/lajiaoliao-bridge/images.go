package main

import (
	"bytes"
	"context"
	"crypto/md5"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"image"
	"image/gif"
	_ "image/jpeg"
	_ "image/png"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/openimsdk/protocol/third"
)

const maxImageBytes = 20 << 20
const maxImagePixels = 40000000
const maxGIFFrames = 120
const maxGIFTotalPixels = 100000000

type pictureInfo struct {
	UUID   string `json:"uuid"`
	Type   string `json:"type"`
	Size   int    `json:"size"`
	Width  int    `json:"width"`
	Height int    `json:"height"`
	URL    string `json:"url"`
}

type pictureContent struct {
	SourcePath string      `json:"sourcePath"`
	Source     pictureInfo `json:"sourcePicture"`
	Big        pictureInfo `json:"bigPicture"`
	Snapshot   pictureInfo `json:"snapshotPicture"`
}

type imageResource struct {
	Content pictureContent
	Expires time.Time
}

type imageLocationError struct {
	Scheme string
	Host   string
}

func (e *imageLocationError) Error() string { return "image_download_host_unverified" }

func imageHost(host string) bool {
	return host == "lj-im-chat-01.lajiaoliao.com" ||
		host == "lj-chat-im-01.lajiaoliao.com" ||
		host == "im-api.lajiaoliao.com" ||
		host == "lj-chat-im-01.oss-accelerate.aliyuncs.com"
}

func imageURL(raw string) bool {
	u, err := url.Parse(raw)
	return err == nil && u.Scheme == "https" && u.User == nil &&
		u.Fragment == "" && (u.Port() == "" || u.Port() == "443") &&
		imageHost(u.Hostname())
}

// Pin every resolved address at dial time, including redirect destinations.
// Account tokens never accompany CDN requests. No proxy environment is used.
func imageClient() *http.Client {
	dialer := &net.Dialer{Timeout: 5 * time.Second}
	return &http.Client{
		Timeout: 45 * time.Second,
		Transport: &http.Transport{
			MaxIdleConns: 2, IdleConnTimeout: 15 * time.Second,
			TLSHandshakeTimeout: 5 * time.Second,
			DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
				host, port, err := net.SplitHostPort(address)
				if err != nil || !imageHost(host) {
					return nil, errors.New("image_address_rejected")
				}
				ips, err := net.DefaultResolver.LookupIPAddr(ctx, host)
				if err != nil || len(ips) == 0 {
					return nil, errors.New("image_dns_failed")
				}
				for _, addr := range ips {
					ip := addr.IP
					if !ip.IsGlobalUnicast() || ip.IsPrivate() || ip.IsLoopback() ||
						ip.IsLinkLocalUnicast() || ip.IsUnspecified() {
						return nil, errors.New("image_address_rejected")
					}
					for _, reserved := range []string{"100.64.0.0/10", "192.0.0.0/24", "192.0.2.0/24", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "240.0.0.0/4", "2001:db8::/32"} {
						_, network, _ := net.ParseCIDR(reserved)
						if network.Contains(ip) {
							return nil, errors.New("image_address_rejected")
						}
					}
				}
				return dialer.DialContext(ctx, network, net.JoinHostPort(ips[0].IP.String(), port))
			},
		},
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			if len(via) != 0 && via[0].Method != http.MethodGet {
				return errors.New("image_upload_redirect_rejected")
			}
			if !imageURL(req.URL.String()) {
				return &imageLocationError{req.URL.Scheme, req.URL.Hostname()}
			}
			if len(via) > 3 {
				return errors.New("image_redirect_rejected")
			}
			return nil
		},
	}
}

func validateImage(raw []byte) (image.Config, string, error) {
	if len(raw) == 0 || len(raw) > maxImageBytes {
		return image.Config{}, "", errors.New("image_size_exceeded")
	}
	config, format, err := image.DecodeConfig(bytes.NewReader(raw))
	if err != nil || (format != "jpeg" && format != "png" && format != "gif") || config.Width <= 0 ||
		config.Height <= 0 || int64(config.Width)*int64(config.Height) > maxImagePixels {
		return image.Config{}, "", errors.New("image_format_or_dimensions_rejected")
	}
	if format == "gif" {
		frames, err := gifFrameCount(raw)
		if err != nil || frames > maxGIFFrames ||
			int64(config.Width)*int64(config.Height)*int64(frames) > maxGIFTotalPixels {
			return image.Config{}, "", errors.New("image_format_or_dimensions_rejected")
		}
		animation, err := gif.DecodeAll(bytes.NewReader(raw))
		if err != nil || len(animation.Image) != frames {
			return image.Config{}, "", errors.New("image_format_or_dimensions_rejected")
		}
		var total int64
		for _, frame := range animation.Image {
			bounds := frame.Bounds()
			total += int64(bounds.Dx()) * int64(bounds.Dy())
			if bounds.Empty() || total > maxGIFTotalPixels {
				return image.Config{}, "", errors.New("image_format_or_dimensions_rejected")
			}
		}
	}
	return config, "image/" + format, nil
}

// Count and bound frames before DecodeAll allocates their pixel buffers.
func gifFrameCount(raw []byte) (int, error) {
	if len(raw) < 13 || string(raw[:6]) != "GIF87a" && string(raw[:6]) != "GIF89a" {
		return 0, errors.New("invalid_gif_header")
	}
	offset := 13
	packed := raw[10]
	if packed&0x80 != 0 {
		offset += 3 * (1 << (uint(packed&0x07) + 1))
	}
	skipBlocks := func() bool {
		for {
			if offset >= len(raw) {
				return false
			}
			size := int(raw[offset])
			offset++
			if size == 0 {
				return true
			}
			if size > len(raw)-offset {
				return false
			}
			offset += size
		}
	}
	frames := 0
	for offset < len(raw) {
		switch raw[offset] {
		case 0x3b:
			offset++
			if offset != len(raw) || frames == 0 {
				return 0, errors.New("invalid_gif_trailer")
			}
			return frames, nil
		case 0x21:
			offset += 2
			if offset > len(raw) || !skipBlocks() {
				return 0, errors.New("invalid_gif_extension")
			}
		case 0x2c:
			if offset+10 > len(raw) {
				return 0, errors.New("invalid_gif_frame")
			}
			left := int(binary.LittleEndian.Uint16(raw[offset+1 : offset+3]))
			top := int(binary.LittleEndian.Uint16(raw[offset+3 : offset+5]))
			width := int(binary.LittleEndian.Uint16(raw[offset+5 : offset+7]))
			height := int(binary.LittleEndian.Uint16(raw[offset+7 : offset+9]))
			canvasWidth := int(binary.LittleEndian.Uint16(raw[6:8]))
			canvasHeight := int(binary.LittleEndian.Uint16(raw[8:10]))
			if width == 0 || height == 0 || left+width > canvasWidth || top+height > canvasHeight {
				return 0, errors.New("invalid_gif_frame_bounds")
			}
			framePacked := raw[offset+9]
			offset += 10
			if framePacked&0x80 != 0 {
				offset += 3 * (1 << (uint(framePacked&0x07) + 1))
			}
			if offset >= len(raw) {
				return 0, errors.New("invalid_gif_frame")
			}
			offset++
			if !skipBlocks() {
				return 0, errors.New("invalid_gif_frame_data")
			}
			frames++
			if frames > maxGIFFrames {
				return frames, nil
			}
		default:
			return 0, errors.New("invalid_gif_block")
		}
	}
	return 0, errors.New("missing_gif_trailer")
}

func (s *server) acquireMedia(ctx context.Context) error {
	s.mediaMu.Lock()
	if s.mediaSlots == nil {
		s.mediaSlots = make(chan struct{}, 2)
	}
	slots := s.mediaSlots
	s.mediaMu.Unlock()
	select {
	case slots <- struct{}{}:
		return nil
	case <-ctx.Done():
		return errors.New("image_timeout")
	}
}

func (s *server) imageUpload(w http.ResponseWriter, r *http.Request) {
	if err := s.acquireMedia(r.Context()); err != nil {
		writeError(w, 408, "image_timeout")
		return
	}
	defer func() { <-s.mediaSlots }()
	r.Body = http.MaxBytesReader(w, r.Body, maxImageBytes+(64<<10))
	reader, err := r.MultipartReader()
	if err != nil {
		writeError(w, 400, "image_multipart_required")
		return
	}
	part, err := reader.NextPart()
	if err != nil || part.FormName() != "image" {
		writeError(w, 400, "image_part_required")
		return
	}
	raw, err := io.ReadAll(io.LimitReader(part, maxImageBytes+1))
	if err != nil {
		writeError(w, 400, "image_read_failed")
		return
	}
	config, mime, err := validateImage(raw)
	if err != nil {
		writeError(w, 422, err.Error())
		return
	}
	if _, err := reader.NextPart(); err != io.EOF {
		writeError(w, 400, "single_image_required")
		return
	}
	tokens, err := s.tokens.load()
	if err != nil || tokens.UserID == "" || tokens.IMToken == "" {
		writeError(w, 401, "session_unavailable")
		return
	}
	uuid := newID()
	location, err := uploadImage(r.Context(), tokens, uuid, mime, raw)
	if err != nil {
		// Upstream errors can contain signed URLs. Return only a fixed local code.
		writeJSON(w, 502, map[string]string{"status": "failed", "error": "image_upload_failed", "stage": imageUploadError(err)})
		return
	}
	pictureType := "png"
	if mime == "image/jpeg" {
		pictureType = "jpg"
	} else if mime == "image/gif" {
		pictureType = "gif"
	}
	info := pictureInfo{uuid, pictureType, len(raw), config.Width, config.Height, location}
	content := pictureContent{"/" + uuid, info, info, info}
	ref := newID()
	s.mediaMu.Lock()
	if s.images == nil {
		s.images = map[string]imageResource{}
	}
	for id, resource := range s.images {
		if time.Now().After(resource.Expires) {
			delete(s.images, id)
		}
	}
	if len(s.images) >= 128 {
		s.mediaMu.Unlock()
		writeError(w, 429, "image_resources_full")
		return
	}
	s.images[ref] = imageResource{content, time.Now().Add(15 * time.Minute)}
	s.mediaMu.Unlock()
	writeJSON(w, 200, map[string]any{"image_ref": ref, "expires_in": 900, "size": len(raw), "width": config.Width, "height": config.Height})
}

func imageUploadError(err error) string {
	for _, prefix := range []string{"image_server_limit", "image_upload_response_invalid",
		"image_upload_part_invalid", "image_upload_parts_missing", "image_upload_host_unverified",
		"image_upload_header_rejected", "image_part_upload_failed", "image_download_host_unverified",
		"upstream status ", "upstream error code "} {
		if strings.HasPrefix(err.Error(), prefix) {
			return prefix
		}
	}
	return "image_upload_transport_failed"
}

func (s *server) imageContent(ref string) ([]byte, error) {
	s.mediaMu.Lock()
	defer s.mediaMu.Unlock()
	resource, ok := s.images[ref]
	if !ok || time.Now().After(resource.Expires) {
		return nil, errors.New("image_ref_expired_or_unknown")
	}
	return json.Marshal(resource.Content)
}

func uploadImage(ctx context.Context, tokens tokenFile, uuid, mime string, raw []byte) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	// Same part-hash algorithm and endpoints as the pinned OpenIM SDK.
	call := func(route string, input any, output any) error {
		raw, err := postJSON(ctx, apiAddr()+route, input, tokens.IMToken)
		if err != nil {
			return err
		}
		var envelope struct {
			Data json.RawMessage `json:"data"`
		}
		if json.Unmarshal(raw, &envelope) != nil {
			return errors.New("image_upload_response_invalid")
		}
		return json.Unmarshal(envelope.Data, output)
	}
	var limits third.PartLimitResp
	if err := call("/object/part_limit", map[string]any{}, &limits); err != nil {
		return "", err
	}
	size := int64(len(raw))
	if limits.MinPartSize <= 0 || limits.MaxPartSize < limits.MinPartSize ||
		limits.MaxNumSize <= 0 || size/limits.MaxPartSize > int64(limits.MaxNumSize) ||
		(size%limits.MaxPartSize != 0 && size/limits.MaxPartSize == int64(limits.MaxNumSize)) {
		return "", errors.New("image_server_limit")
	}
	partSize := max(limits.MinPartSize, (size+int64(limits.MaxNumSize)-1)/int64(limits.MaxNumSize))
	partCount := int((size + partSize - 1) / partSize)
	if partCount < 1 || partCount > 10000 {
		return "", errors.New("image_server_limit")
	}
	hashes := make([]string, partCount)
	for i := range hashes {
		sum := md5.Sum(raw[int64(i)*partSize : min(int64(i+1)*partSize, size)])
		hashes[i] = hex.EncodeToString(sum[:])
	}
	sum := md5.Sum([]byte(strings.Join(hashes, ",")))
	name := tokens.UserID + "/" + uuid
	var initiated third.InitiateMultipartUploadResp
	if err := call("/object/initiate_multipart_upload", &third.InitiateMultipartUploadReq{
		Hash: hex.EncodeToString(sum[:]), Size: size, PartSize: partSize,
		MaxParts: -1, Name: name, ContentType: mime, Cause: "image",
	}, &initiated); err != nil {
		return "", err
	}
	location := initiated.Url
	if initiated.Upload != nil {
		upload := initiated.Upload
		if upload.PartSize != partSize || upload.Sign == nil || upload.UploadID == "" {
			return "", errors.New("image_upload_response_invalid")
		}
		client := imageClient()
		defer client.CloseIdleConnections()
		seen := map[int32]bool{}
		for _, part := range upload.Sign.Parts {
			if part == nil || part.PartNumber < 1 || part.PartNumber > int32(partCount) || seen[part.PartNumber] {
				return "", errors.New("image_upload_part_invalid")
			}
			seen[part.PartNumber] = true
			address := part.Url
			if address == "" {
				address = upload.Sign.Url
			}
			if !imageURL(address) {
				return "", errors.New("image_upload_host_unverified")
			}
			u, _ := url.Parse(address)
			query := u.Query()
			for _, kv := range append(upload.Sign.Query, part.Query...) {
				query[kv.Key] = kv.Values
			}
			u.RawQuery = query.Encode()
			begin := int64(part.PartNumber-1) * partSize
			payload := raw[begin:min(begin+partSize, size)]
			req, err := http.NewRequestWithContext(ctx, http.MethodPut, u.String(), bytes.NewReader(payload))
			if err != nil {
				return "", err
			}
			for _, kv := range append(upload.Sign.Header, part.Header...) {
				if strings.EqualFold(kv.Key, "token") || strings.EqualFold(kv.Key, "host") {
					return "", errors.New("image_upload_header_rejected")
				}
				req.Header[kv.Key] = kv.Values
			}
			req.ContentLength = int64(len(payload))
			resp, err := client.Do(req)
			if err != nil {
				return "", errors.New("image_part_upload_failed")
			}
			io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
			resp.Body.Close()
			if resp.StatusCode < 200 || resp.StatusCode >= 300 {
				return "", errors.New("image_part_upload_failed_" + strconv.Itoa(resp.StatusCode))
			}
		}
		if len(seen) != partCount {
			return "", errors.New("image_upload_parts_missing")
		}
		var complete third.CompleteMultipartUploadResp
		if err := call("/object/complete_multipart_upload", &third.CompleteMultipartUploadReq{
			UploadID: upload.UploadID, Parts: hashes, Name: name, ContentType: mime, Cause: "image",
		}, &complete); err != nil {
			return "", err
		}
		location = complete.Url
	}
	if !imageURL(location) {
		u, err := url.Parse(location)
		if err != nil {
			return "", errors.New("image_download_host_unverified")
		}
		return "", &imageLocationError{u.Scheme, u.Hostname()}
	}
	return location, nil
}
