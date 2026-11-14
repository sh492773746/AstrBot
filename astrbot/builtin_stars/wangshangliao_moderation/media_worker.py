"""One-image local QR/OCR worker; no network, retained files or recognized text."""

import csv
import io
import json
import re
import subprocess
import sys
import time
import unicodedata
import warnings

from PIL import Image, ImageOps


def main():
    """Inspect bounded bitmap bytes and emit booleans, never QR/OCR content.

    Raises:
        ValueError: If native dimensions or the bitmap format are invalid.
        RuntimeError: If the required local recognizer is unavailable.
    """
    import cv2
    import numpy as np

    cv2.setNumThreads(1)
    raw = sys.stdin.buffer.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        raise ValueError("media_image_limit")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(raw)) as original:
            if (
                not 1 <= original.width <= 16384
                or not 1 <= original.height <= 16384
                or original.width * original.height > 16_000_000
                or any(
                    expected and actual != expected
                    for actual, expected in zip(
                        original.size, (int(sys.argv[1]), int(sys.argv[2])), strict=True
                    )
                )
            ):
                raise ValueError("media_image_invalid")
            if original.format not in {"JPEG", "PNG", "WEBP", "GIF", "BMP"}:
                raise ValueError("media_image_unsupported_format")
            if getattr(original, "is_animated", False):
                raise ValueError("media_image_unsupported_animation")
            bitmap = ImageOps.exif_transpose(original).convert("RGB")
    bitmap.thumbnail((2000, 2000))
    gray = cv2.cvtColor(np.asarray(bitmap), cv2.COLOR_RGB2GRAY)
    # Decode, rather than accepting three square-like objects as a QR code.
    qr = False
    if sys.argv[3] == "1":
        decoded, _, _ = cv2.QRCodeDetector().detectAndDecode(gray)
        qr = bool(decoded)
    if qr:
        print(json.dumps({"qr": True, "digits": False}))
        return
    if sys.argv[4] != "1":
        print(json.dumps({"qr": False, "digits": False}))
        return
    # Full English recognition avoids a digit-only whitelist turning letters
    # into false numbers. TSV confidence and an actual digit are both required.
    # Outlined colored digits can be low confidence on the original bitmap.
    # Try one Otsu view without reducing confidence or extending the CPU budget.
    deadline = time.monotonic() + 6
    digits = False
    timed_out = False
    for binary in (False, True):
        buffer = io.BytesIO()
        if binary:
            _, threshold = cv2.threshold(
                gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
            )
            Image.fromarray(threshold).save(buffer, "PNG")
        else:
            bitmap.save(buffer, "PNG")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        try:
            result = subprocess.run(
                ["tesseract", "stdin", "stdout", "-l", "eng", "--psm", "11", "tsv"],
                input=buffer.getvalue(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=min(3, remaining),
                check=True,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            continue
        if len(result.stdout) > 1024 * 1024:
            raise ValueError("media_ocr_limit")
        digits = any(
            float(row.get("conf", "-1")) >= 80
            and re.search(r"[0-9]", unicodedata.normalize("NFKC", row.get("text", "")))
            for row in csv.DictReader(
                io.StringIO(result.stdout.decode("utf-8")), delimiter="\t"
            )
        )
        if digits:
            break
    if timed_out and not digits:
        raise RuntimeError("media_ocr_timeout")
    print(json.dumps({"qr": False, "digits": bool(digits)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        reason = str(exc)
        if reason not in {
            "media_image_invalid",
            "media_image_limit",
            "media_image_unsupported_format",
            "media_image_unsupported_animation",
            "media_ocr_limit",
            "media_ocr_timeout",
        }:
            reason = (
                "media_recognizer_unavailable"
                if isinstance(exc, (ImportError, FileNotFoundError))
                else "media_ocr_failed"
                if isinstance(exc, subprocess.CalledProcessError)
                else "media_recognizer_failed"
            )
        print(json.dumps({"error": reason}))
        sys.exit(1)
