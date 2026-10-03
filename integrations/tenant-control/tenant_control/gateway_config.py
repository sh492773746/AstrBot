"""Resolve an approved AstrBot model without exposing source credentials to tenants."""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit


def chat_from_astrbot(config: dict, provider_id: str) -> dict:
    matches = [p for p in config.get("provider", []) if p.get("id") == provider_id]
    if len(matches) != 1 or not matches[0].get("enable", False):
        raise ValueError("Approved chat provider is missing or disabled")
    provider = matches[0]
    source_id = provider.get("provider_source_id")
    sources = [
        p for p in config.get("provider_sources", []) if p.get("id") == source_id
    ]
    if len(sources) != 1 or sources[0].get("type") != "openai_chat_completion":
        raise ValueError("Expected exactly one OpenAI-compatible chat source")
    source = sources[0]
    if source.get("enable") is False:
        raise ValueError("Approved provider source is disabled")
    base = source.get("api_base", "").rstrip("/")
    url = urlsplit(base)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("Approved provider needs a credential-free HTTPS base URL")
    keys = source.get("key")
    if (
        not isinstance(keys, list)
        or not keys
        or not isinstance(keys[0], str)
        or not keys[0]
    ):
        raise ValueError("Approved provider has no usable key")
    if not isinstance(provider.get("model"), str) or not provider["model"]:
        raise ValueError("Approved provider has no model")
    return {
        "url": base + "/chat/completions",
        "key": keys[0],
        "model": provider["model"],
        "provider_id": provider_id,
    }


def load_gateway_config() -> dict | None:
    directory = os.getenv("CREDENTIALS_DIRECTORY")
    if not directory:
        return None
    path = Path(directory) / "gateway.json"
    # systemd LoadCredential mounts may be root:root 0440 inside the service's
    # private credential directory. Never accept world access or group writes.
    mode = path.stat().st_mode if path.exists() else 0
    if (
        path.is_symlink()
        or not path.is_file()
        or mode & 0o027
        or (mode & 0o040 and path.stat().st_gid != 0)
    ):
        raise ValueError("Gateway credential must be a private regular file")
    return json.loads(path.read_text())
