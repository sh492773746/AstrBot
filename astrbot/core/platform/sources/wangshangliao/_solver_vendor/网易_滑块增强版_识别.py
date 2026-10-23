from __future__ import annotations

import argparse
import base64
import json
import os
import urllib.request
from urllib.error import URLError
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np

try:
    cv2.setNumThreads(max(1, int(os.environ.get("YIDUN_CV_THREADS", "1"))))
    cv2.ocl.setUseOpenCL(False)
except Exception:
    pass


ROOT = Path(__file__).resolve().parent
DEFAULT_OUT_BASE = ROOT / "recognition_runs"


def asset_dir() -> Path:
    for candidate in (ROOT / "网易_滑块增强版_资源", ROOT / "netease_slide_pro", ROOT.parent / "模型"):
        if candidate.exists():
            return candidate
    return ROOT / "网易_滑块增强版_资源"


ASSET_DIR = asset_dir()
MODEL_PATH = ASSET_DIR / "best.pt"
FP16_ONNX_MODEL_PATH = ASSET_DIR / "best.fp16.onnx"
ONNX_MODEL_PATH = ASSET_DIR / "best.onnx"
CANVAS_WIDTH = 320
SEGMENT_IMAGE_SIZE = 320
SEGMENT_BOX_CLASS = 0
SEGMENT_CONF_THRESHOLD = 0.25
SEGMENT_IOU_THRESHOLD = 0.70
SEGMENT_MASK_THRESHOLD = 0.50
SLIDER_WIDTH = 40
JIGSAW_WIDTH = 61
DRAG_X_MAX = 280
EDGE_CLAMP_RAW_X = 259
EDGE_PRECLAMP_PROMOTE_RAW_X = 258
EDGE_PRECLAMP_PROMOTE_MIN_SCORE = 0.55
EDGE_PRECLAMP_PLATEAU_MIN_SCORE = 0.55
EDGE_PRECLAMP_PLATEAU_MIN_RATIO = 0.92
EDGE_FALLBACK_MAX_RAW_X = 249
EDGE_FALLBACK_MIN_SCORE = 0.60
EDGE_FALLBACK_MIN_RATIO = 0.97

SAMPLE = {
    "attrs": 1.056674736787752,
    "bg": "https://necaptcha.nosdn.127.net/aecbd506f959442baf5ed12ef4b0f441.jpg",
    "front": "https://necaptcha.nosdn.127.net/01505f49a99d4daaa851b9f9c1c8b0d0.png",
    "token": "",
    "type": 2,
    "waitTime": 300,
    "zoneId": "CN31",
}


class DnnSegmentModel:
    backend = "opencv_dnn"

    def __init__(self, model_path: Path):
        self.model_path = Path(model_path)
        self.net = cv2.dnn.readNetFromONNX(np.fromfile(str(self.model_path), dtype=np.uint8))
        self.output_names = self.net.getUnconnectedOutLayersNames()

    def forward(self, image: np.ndarray):
        input_image, ratio, pad = letterbox_image(image, (SEGMENT_IMAGE_SIZE, SEGMENT_IMAGE_SIZE))
        blob = input_image[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        self.net.setInput(blob)
        return self.net.forward(self.output_names), ratio, pad


def load_model(backend: str | None = None):
    backend = (backend or os.environ.get("YIDUN_MODEL_BACKEND") or "auto").strip().lower()
    onnx_candidates = [FP16_ONNX_MODEL_PATH, ONNX_MODEL_PATH]
    if backend in {"auto", "onnx", "dnn", "opencv", "opencv_dnn"}:
        onnx_path = next((path for path in onnx_candidates if path.exists()), None)
        if onnx_path is not None:
            return DnnSegmentModel(onnx_path)
    if backend in {"onnx", "dnn", "opencv", "opencv_dnn"}:
        paths = ", ".join(str(path) for path in onnx_candidates)
        raise FileNotFoundError(f"ONNX model not found; tried: {paths}")
    if not MODEL_PATH.exists():
        raise FileNotFoundError(f"PyTorch model not found: {MODEL_PATH}")
    from ultralytics import YOLO

    return YOLO(str(MODEL_PATH))


def warmup_model(model) -> None:
    try:
        if isinstance(model, DnnSegmentModel):
            model.forward(np.zeros((160, 320, 3), dtype=np.uint8))
        else:
            model.predict(np.zeros((160, 320, 3), dtype=np.uint8), verbose=False, imgsz=SEGMENT_IMAGE_SIZE)
    except Exception:
        pass


def url_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return [str(value)] if value else []


def normalize_proxy_url(proxy: str | None) -> str:
    proxy = str(proxy or "").strip()
    if not proxy:
        return ""
    if "://" not in proxy:
        return f"http://{proxy}"
    return proxy


def normalize_sample(sample: dict) -> dict:
    if "data" in sample and isinstance(sample["data"], dict):
        sample = sample["data"]

    attrs = sample.get("attrs", 0.0)
    if isinstance(attrs, list):
        attrs = attrs[0] if attrs else 0.0
    if attrs in (None, ""):
        attrs = 0.0

    bg = url_list(sample["bg"])
    front = url_list(sample["front"])

    token = sample.get("token") or Path(bg[0]).stem
    return {
        "attrs": float(attrs),
        "bg": bg,
        "front": front,
        "token": token,
        "type": sample.get("type", 2),
        "waitTime": sample.get("waitTime", 300),
        "zoneId": sample.get("zoneId", "CN31"),
    }


def load_samples(input_path: Path | None) -> list[dict]:
    if input_path is None:
        return [normalize_sample(SAMPLE)]

    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "samples" in payload:
        payload = payload["samples"]
    elif isinstance(payload, dict):
        payload = [payload]

    return [normalize_sample(item) for item in payload]


def proxy_opener(proxy: str | None = None):
    proxy_url = normalize_proxy_url(proxy)
    if proxy_url:
        handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    else:
        # Avoid broken Windows/system proxy settings when the request asks for direct traffic.
        handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(handler)


def download_bytes(url: str, proxy: str | None = None) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
    )
    with proxy_opener(proxy).open(request, timeout=20) as resp:
        return resp.read()


def download_from_candidates(urls, proxy: str | None = None) -> bytes:
    errors = []
    for url in url_list(urls):
        try:
            return download_bytes(url, proxy=proxy)
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    proxy_desc = normalize_proxy_url(proxy) or "direct"
    raise URLError(f"download failed via {proxy_desc}; tried {len(errors)} url(s): {' | '.join(errors)}")


def download(urls, path: Path, proxy: str | None = None) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = download_from_candidates(urls, proxy=proxy)
    path.write_bytes(data)
    return data


def decode_image(data: bytes, flags: int = cv2.IMREAD_COLOR) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), flags)
    if image is None:
        raise RuntimeError("failed to decode image bytes")
    return image


def read_image(path: Path, flags: int = cv2.IMREAD_COLOR) -> np.ndarray:
    try:
        return decode_image(path.read_bytes(), flags)
    except RuntimeError as exc:
        raise RuntimeError(f"failed to decode image: {path}") from exc


def sigmoid_array(value: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(value, -80.0, 80.0)))


def letterbox_image(image: np.ndarray, new_shape: tuple[int, int], color: tuple[int, int, int] = (114, 114, 114)):
    height, width = image.shape[:2]
    ratio = min(new_shape[0] / height, new_shape[1] / width)
    resized_width = int(round(width * ratio))
    resized_height = int(round(height * ratio))
    pad_width = (new_shape[1] - resized_width) / 2
    pad_height = (new_shape[0] - resized_height) / 2
    if (width, height) != (resized_width, resized_height):
        image = cv2.resize(image, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR)
    top = int(round(pad_height - 0.1))
    bottom = int(round(pad_height + 0.1))
    left = int(round(pad_width - 0.1))
    right = int(round(pad_width + 0.1))
    return cv2.copyMakeBorder(image, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color), ratio, (left, top)


def xywh_to_xyxy(boxes: np.ndarray) -> np.ndarray:
    converted = boxes.copy()
    converted[:, 0] = boxes[:, 0] - boxes[:, 2] / 2
    converted[:, 1] = boxes[:, 1] - boxes[:, 3] / 2
    converted[:, 2] = boxes[:, 0] + boxes[:, 2] / 2
    converted[:, 3] = boxes[:, 1] + boxes[:, 3] / 2
    return converted


def mask_to_target_image(source_image: np.ndarray, binary_mask: np.ndarray) -> np.ndarray:
    target_image = np.zeros_like(source_image)
    binary_mask_3ch = cv2.cvtColor(binary_mask, cv2.COLOR_GRAY2BGR)
    masked_region = cv2.bitwise_and(source_image, source_image, mask=binary_mask)
    target_image = np.where(binary_mask_3ch == 255, masked_region, target_image)
    _, target_image = cv2.threshold(target_image, 0, 255, cv2.THRESH_BINARY)
    return target_image


def segment_dnn(model: DnnSegmentModel, source_image: np.ndarray) -> np.ndarray:
    outputs, ratio, (pad_left, pad_top) = model.forward(source_image)
    if len(outputs) < 2:
        raise RuntimeError(f"unexpected ONNX output count: {len(outputs)}")

    pred = outputs[0][0].T
    proto = outputs[1][0]
    mask_dim = int(proto.shape[0])
    class_count = int(pred.shape[1] - 4 - mask_dim)
    if class_count <= SEGMENT_BOX_CLASS:
        raise RuntimeError(f"unexpected ONNX prediction shape: {pred.shape}")

    boxes = xywh_to_xyxy(pred[:, :4])
    class_scores = pred[:, 4 : 4 + class_count]
    classes = class_scores.argmax(axis=1)
    scores = class_scores.max(axis=1)
    keep = (classes == SEGMENT_BOX_CLASS) & (scores >= SEGMENT_CONF_THRESHOLD)
    boxes = boxes[keep]
    scores = scores[keep]
    coeffs = pred[keep, 4 + class_count :]
    if len(boxes) == 0:
        return np.zeros_like(source_image)

    nms_boxes = [[float(x1), float(y1), float(x2 - x1), float(y2 - y1)] for x1, y1, x2, y2 in boxes]
    indexes = cv2.dnn.NMSBoxes(nms_boxes, scores.astype(float).tolist(), SEGMENT_CONF_THRESHOLD, SEGMENT_IOU_THRESHOLD)
    if len(indexes) == 0:
        return np.zeros_like(source_image)

    indexes = np.array(indexes).reshape(-1)
    boxes = boxes[indexes]
    coeffs = coeffs[indexes]
    proto_flat = proto.reshape(mask_dim, -1)
    masks = sigmoid_array(coeffs @ proto_flat).reshape(-1, proto.shape[1], proto.shape[2])

    combined = np.zeros((SEGMENT_IMAGE_SIZE, SEGMENT_IMAGE_SIZE), dtype=np.uint8)
    for mask, box in zip(masks, boxes):
        upscaled_mask = cv2.resize(mask, (SEGMENT_IMAGE_SIZE, SEGMENT_IMAGE_SIZE), interpolation=cv2.INTER_LINEAR)
        x1, y1, x2, y2 = box.astype(int)
        x1 = max(0, min(SEGMENT_IMAGE_SIZE - 1, x1))
        y1 = max(0, min(SEGMENT_IMAGE_SIZE - 1, y1))
        x2 = max(0, min(SEGMENT_IMAGE_SIZE, x2))
        y2 = max(0, min(SEGMENT_IMAGE_SIZE, y2))
        if x2 <= x1 or y2 <= y1:
            continue
        binary_crop = (upscaled_mask[y1:y2, x1:x2] > SEGMENT_MASK_THRESHOLD).astype(np.uint8) * 255
        combined[y1:y2, x1:x2] = np.maximum(combined[y1:y2, x1:x2], binary_crop)

    height, width = source_image.shape[:2]
    unpadded_height = int(round(height * ratio))
    unpadded_width = int(round(width * ratio))
    binary_mask = combined[pad_top : pad_top + unpadded_height, pad_left : pad_left + unpadded_width]
    if binary_mask.shape[:2] != (height, width):
        binary_mask = cv2.resize(binary_mask, (width, height), interpolation=cv2.INTER_NEAREST)
    return mask_to_target_image(source_image, binary_mask)


def segment_ultralytics(model, source_image: np.ndarray) -> np.ndarray:
    target_image = np.zeros_like(source_image)
    results = model.predict(source_image, verbose=False, imgsz=SEGMENT_IMAGE_SIZE)
    for result in results:
        if result.masks is None or result.boxes is None:
            continue
        for mask, box in zip(result.masks, result.boxes):
            if int(box.cls.item()) != 0:
                continue
            mask_np = mask.cpu().data.numpy().squeeze()
            mask_np = (mask_np * 255).astype(np.uint8)
            if mask_np.shape != source_image.shape[:2]:
                mask_np = cv2.resize(mask_np, (source_image.shape[1], source_image.shape[0]))
            _, binary_mask = cv2.threshold(mask_np, 127, 255, cv2.THRESH_BINARY)
            masked_region = cv2.bitwise_and(source_image, source_image, mask=binary_mask)
            binary_mask_3ch = cv2.cvtColor(binary_mask, cv2.COLOR_GRAY2BGR)
            target_image = np.where(binary_mask_3ch == 255, masked_region, target_image)
            _, target_image = cv2.threshold(target_image, 0, 255, cv2.THRESH_BINARY)
    return target_image


def segment(model, source_image: np.ndarray) -> np.ndarray:
    if isinstance(model, DnnSegmentModel):
        return segment_dnn(model, source_image)
    return segment_ultralytics(model, source_image)


def process_slider(slider_image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(slider_image, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 25, 255, cv2.THRESH_BINARY)
    return cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)


def find_and_crop_largest_region(image: np.ndarray):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    _, binary = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise RuntimeError("no contour found in slider image")
    max_contour = max(contours, key=cv2.contourArea)
    x, y, w, h = cv2.boundingRect(max_contour)
    height, width = image.shape[:2]
    x0 = max(0, x - 5)
    y0 = max(0, y - 5)
    x1 = min(width, x + w + 5)
    y1 = min(height, y + h + 5)
    return image[y0:y1, x0:x1], (x0, y0, x1 - x0, y1 - y0)


def restrict_drag(drag_x: float, offset: int = SLIDER_WIDTH - JIGSAW_WIDTH) -> float:
    start_left = 0
    max_left = CANVAS_WIDTH - JIGSAW_WIDTH
    left = start_left + drag_x
    threshold = -offset if offset < 0 else offset / 2
    if drag_x <= threshold:
        delta = drag_x
        left += -delta / 2 if offset < 0 else delta
    elif CANVAS_WIDTH - drag_x - SLIDER_WIDTH <= threshold:
        delta = drag_x - (CANVAS_WIDTH - SLIDER_WIDTH - threshold)
        left += (offset / 2 - delta / 2) if offset < 0 else (offset / 2 + delta)
    else:
        left += offset / 2
    if left <= start_left:
        left = start_left
    if left >= max_left:
        left = max_left
    return left


def jigsaw_candidates(attrs: float) -> list[dict]:
    ratio = (CANVAS_WIDTH / 2 - JIGSAW_WIDTH) / CANVAS_WIDTH
    out = []
    for raw_x in range(DRAG_X_MAX + 1):
        left = restrict_drag(raw_x) * ratio
        out.append(
            {
                "RawX": raw_x,
                "position": f"{left}px",
                "rotation": f"rotate({attrs * left}deg)",
            }
        )
    return out


def template_detection(ctx, bg_mask: np.ndarray, slider_mask: np.ndarray, attrs: float, debug_dir: Path, save_artifacts: bool = True):
    initial_rotation_center_x = 60
    rotation_center_y = 158 if attrs > 0 else 0
    slider_crop, max_bbox = find_and_crop_largest_region(slider_mask)
    x, y, w, h = max_bbox
    slider_h, slider_w = slider_crop.shape[:2]
    full_slider_h, full_slider_w = slider_mask.shape[:2]
    if save_artifacts:
        debug_dir.mkdir(parents=True, exist_ok=True)

    best_raw_x = None
    highest_similarity = -1
    best_item = None
    best_pair = None
    scores = []

    def score_candidates(use_full_warp: bool = False, raw_x_filter: set[int] | None = None):
        out = []
        for item in jigsaw_candidates(attrs):
            if raw_x_filter is not None and int(item["RawX"]) not in raw_x_filter:
                continue
            rotation_angle = float(item["rotation"].replace("rotate(", "").replace("deg)", ""))
            position_offset = float(item["position"].replace("px", ""))
            rotation_center_x = initial_rotation_center_x + position_offset
            rotation_matrix = cv2.getRotationMatrix2D((rotation_center_x, rotation_center_y), rotation_angle, 1.0)
            left_crop = int(rotation_center_x)

            base_x0 = left_crop - full_slider_w
            if base_x0 < 0 or left_crop > bg_mask.shape[1]:
                continue
            if x < 0 or y < 0 or x + w > full_slider_w or y + h > full_slider_h:
                continue
            crop_x0 = base_x0 + x
            crop_y0 = y
            if crop_x0 < 0 or crop_x0 + w > bg_mask.shape[1] or crop_y0 + h > bg_mask.shape[0]:
                continue

            if use_full_warp:
                rotated_image = cv2.warpAffine(bg_mask, rotation_matrix, (bg_mask.shape[1], bg_mask.shape[0]))
                cropped_image = rotated_image[:, base_x0:left_crop]
                cropped_image = cropped_image[y : y + h, x : x + w]
            else:
                crop_matrix = rotation_matrix.copy()
                crop_matrix[0, 2] -= crop_x0
                crop_matrix[1, 2] -= crop_y0
                cropped_image = cv2.warpAffine(bg_mask, crop_matrix, (w, h))

            if cropped_image.shape[0] < slider_h or cropped_image.shape[1] < slider_w:
                continue

            score_map = cv2.matchTemplate(cropped_image, slider_crop, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(score_map)
            out.append((item, float(max_val), cropped_image))
        return out

    candidate_scores = score_candidates(use_full_warp=False)
    min_expected_candidates = 200
    if len(candidate_scores) < min_expected_candidates:
        candidate_scores = score_candidates(use_full_warp=True)
    else:
        top_raw_xs = {int(item["RawX"]) for item, _, _ in sorted(candidate_scores, key=lambda row: row[1], reverse=True)[:24]}
        verified_scores = score_candidates(use_full_warp=True, raw_x_filter=top_raw_xs)
        if verified_scores:
            score_by_raw_x = {int(item["RawX"]): (item, score, image) for item, score, image in candidate_scores}
            for item, score, image in verified_scores:
                score_by_raw_x[int(item["RawX"])] = (item, score, image)
            candidate_scores = [score_by_raw_x[int(item["RawX"])] for item, _, _ in candidate_scores]

    for item, max_val, cropped_image in candidate_scores:
        scores.append({"RawX": item["RawX"], "score": max_val, "position": item["position"], "rotation": item["rotation"]})
        if max_val > highest_similarity:
            highest_similarity = max_val
            best_raw_x = item["RawX"]
            best_item = item
            best_pair = (cropped_image.copy(), slider_crop.copy())

    if best_item is None:
        raise RuntimeError(
            "no match result produced; "
            f"slider_bbox={max_bbox}, slider_shape={slider_mask.shape[:2]}, bg_shape={bg_mask.shape[:2]}"
        )

    if int(best_raw_x) == EDGE_PRECLAMP_PROMOTE_RAW_X and highest_similarity >= EDGE_PRECLAMP_PROMOTE_MIN_SCORE:
        plateau_scores = [
            row for row in candidate_scores
            if int(row[0]["RawX"]) == EDGE_CLAMP_RAW_X
        ]
        if plateau_scores:
            plateau_item, plateau_score, plateau_image = plateau_scores[0]
            plateau_ratio = plateau_score / max(highest_similarity, 1e-6)
            if plateau_score >= EDGE_PRECLAMP_PLATEAU_MIN_SCORE and plateau_ratio >= EDGE_PRECLAMP_PLATEAU_MIN_RATIO:
                original_raw_x = int(best_item["RawX"])
                best_item = dict(plateau_item)
                best_item["edgePlateauPromoteFromRawX"] = original_raw_x
                best_item["edgePlateauPromoteFromScore"] = highest_similarity
                best_item["edgePlateauScore"] = plateau_score
                best_item["edgePlateauRatio"] = plateau_ratio
                highest_similarity = max(highest_similarity, plateau_score)
                best_pair = (plateau_image.copy(), slider_crop.copy())

    if int(best_raw_x) >= EDGE_CLAMP_RAW_X:
        non_edge_scores = [
            row for row in candidate_scores
            if int(row[0]["RawX"]) <= EDGE_FALLBACK_MAX_RAW_X
        ]
        if non_edge_scores:
            non_edge_item, non_edge_score, non_edge_image = max(non_edge_scores, key=lambda row: row[1])
            ratio = non_edge_score / max(highest_similarity, 1e-6)
            if non_edge_score >= EDGE_FALLBACK_MIN_SCORE and ratio >= EDGE_FALLBACK_MIN_RATIO:
                original_raw_x = int(best_item["RawX"])
                best_item = dict(non_edge_item)
                best_item["edgeFallbackFromRawX"] = original_raw_x
                best_item["edgeFallbackFromScore"] = highest_similarity
                best_item["edgeFallbackRatio"] = ratio
                highest_similarity = non_edge_score
                best_pair = (non_edge_image.copy(), slider_crop.copy())

    if save_artifacts and best_pair is not None:
        cv2.imwrite(str(debug_dir / "best_match_pair.png"), cv2.hconcat(list(best_pair)))
    return best_item, highest_similarity, max_bbox, scores


def draw_image(background_image: np.ndarray, overlay_image: np.ndarray, attrs: float, position_data: dict) -> np.ndarray:
    position_offset = float(position_data["position"].replace("px", ""))
    rotation_angle = float(position_data["rotation"].replace("rotate(", "").replace("deg)", ""))
    overlay_image[:1, :] = [0, 0, 255, 255]
    overlay_image[-1:, :] = [0, 0, 255, 255]
    overlay_image[:, :1] = [0, 0, 255, 255]
    overlay_image[:, -1:] = [0, 0, 255, 255]
    overlay_image = cv2.copyMakeBorder(overlay_image, 0, 0, 0, 150, cv2.BORDER_CONSTANT, value=(0, 0, 0, 0))

    rotation_center_y = 160 if attrs > 0 else 0
    center_of_rotation = (overlay_image.shape[1] - 150, rotation_center_y)
    rotation_matrix = cv2.getRotationMatrix2D(center_of_rotation, -rotation_angle, 1)
    rotated_image = cv2.warpAffine(
        overlay_image,
        rotation_matrix,
        (overlay_image.shape[1], overlay_image.shape[0]),
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0, 0),
    )

    transparent_background = np.zeros((160, 320, 4), dtype=np.uint8)
    x_offset = min(transparent_background.shape[1] - rotated_image.shape[1], int(position_offset))
    transparent_background[0 : rotated_image.shape[0], x_offset : x_offset + rotated_image.shape[1]] = rotated_image
    bg_rgb = transparent_background[:, :, :3]
    alpha = transparent_background[:, :, 3]
    background_with_alpha = cv2.cvtColor(background_image, cv2.COLOR_BGR2BGRA)
    for channel in range(3):
        background_with_alpha[:, :, channel] = (
            alpha / 255.0 * bg_rgb[:, :, channel] + (1 - alpha / 255.0) * background_with_alpha[:, :, channel]
        )
    return background_with_alpha


def png_b64(image: np.ndarray) -> str:
    ok, buffer = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError("failed to encode overlay image")
    return base64.b64encode(buffer.tobytes()).decode("ascii")


def run_sample(
    model,
    ctx,
    sample: dict,
    out_base: Path,
    save_artifacts: bool = True,
    render_overlay: bool = True,
    proxy: str | None = None,
    inference_lock=None,
) -> dict:
    out_dir = out_base / sample["token"]
    bg_path = out_dir / "background.jpg"
    slider_path = out_dir / "slider.png"
    with ThreadPoolExecutor(max_workers=2) as executor:
        if save_artifacts:
            out_dir.mkdir(parents=True, exist_ok=True)
            bg_future = executor.submit(download, sample["bg"], bg_path, proxy)
            slider_future = executor.submit(download, sample["front"], slider_path, proxy)
        else:
            bg_future = executor.submit(download_from_candidates, sample["bg"], proxy)
            slider_future = executor.submit(download_from_candidates, sample["front"], proxy)
        bg_bytes = bg_future.result()
        slider_bytes = slider_future.result()

    bg = decode_image(bg_bytes)
    slider = decode_image(slider_bytes)

    if inference_lock is None:
        bg_mask = segment(model, bg)
    else:
        with inference_lock:
            bg_mask = segment(model, bg)
    slider_mask = process_slider(slider)
    if save_artifacts:
        cv2.imwrite(str(out_dir / "background_mask.png"), bg_mask)
        cv2.imwrite(str(out_dir / "slider_mask.png"), slider_mask)

    best_item, score, max_bbox, scores = template_detection(
        ctx,
        bg_mask,
        slider_mask,
        sample["attrs"],
        out_dir / "debug",
        save_artifacts=save_artifacts,
    )
    overlay_png_b64 = ""
    if render_overlay or save_artifacts:
        slider_rgba = decode_image(slider_bytes, cv2.IMREAD_UNCHANGED)
        overlay = draw_image(bg, slider_rgba, sample["attrs"], best_item)
        overlay_png_b64 = png_b64(overlay) if render_overlay else ""
        if save_artifacts:
            cv2.imwrite(str(out_dir / "overlay_result.png"), overlay)

    result = {
        "input": sample,
        "result": {
            "RawX": best_item["RawX"],
            "position": best_item["position"],
            "rotation": best_item["rotation"],
            "similarity": score,
            "max_bbox": max_bbox,
            "edgeFallbackFromRawX": best_item.get("edgeFallbackFromRawX"),
            "edgeFallbackFromScore": best_item.get("edgeFallbackFromScore"),
            "edgeFallbackRatio": best_item.get("edgeFallbackRatio"),
            "edgeClampFromRawX": best_item.get("edgeClampFromRawX"),
            "edgeClampSubmitRawX": best_item.get("edgeClampSubmitRawX"),
            "edgePlateauPromoteFromRawX": best_item.get("edgePlateauPromoteFromRawX"),
            "edgePlateauPromoteFromScore": best_item.get("edgePlateauPromoteFromScore"),
            "edgePlateauScore": best_item.get("edgePlateauScore"),
            "edgePlateauRatio": best_item.get("edgePlateauRatio"),
        },
        "top_scores": sorted(scores, key=lambda x: x["score"], reverse=True)[:10],
        "overlay_png_b64": overlay_png_b64,
        "files": {
            "background": str(bg_path) if save_artifacts else "",
            "slider": str(slider_path) if save_artifacts else "",
            "background_mask": str(out_dir / "background_mask.png") if save_artifacts else "",
            "slider_mask": str(out_dir / "slider_mask.png") if save_artifacts else "",
            "overlay_result": str(out_dir / "overlay_result.png") if save_artifacts else "",
            "best_match_pair": str(out_dir / "debug" / "best_match_pair.png") if save_artifacts else "",
        },
    }
    if save_artifacts:
        file_result = dict(result)
        file_result["overlay_png_b64"] = ""
        (out_dir / "result.json").write_text(json.dumps(file_result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--input", type=Path, help="JSON file containing one sample, a list of samples, or {samples: [...]}.")
    parser.add_argument("-o", "--out-dir", type=Path, default=DEFAULT_OUT_BASE)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    samples = load_samples(args.input)
    if args.limit is not None:
        samples = samples[: args.limit]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    model = load_model()
    ctx = None

    batch = []
    for index, sample in enumerate(samples, 1):
        print(f"[{index}/{len(samples)}] token={sample['token']} attrs={sample['attrs']}")
        result = run_sample(model, ctx, sample, args.out_dir)
        batch.append(
            {
                "token": sample["token"],
                "attrs": sample["attrs"],
                "bg": sample["bg"],
                "front": sample["front"],
                **result["result"],
                "overlay_result": result["files"]["overlay_result"],
                "best_match_pair": result["files"]["best_match_pair"],
            }
        )

    summary = {"count": len(batch), "samples": batch}
    (args.out_dir / "batch_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
