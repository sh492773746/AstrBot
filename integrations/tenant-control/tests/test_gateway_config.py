"""Provider resolution never exports secrets into the tenant configuration."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tenant_control.gateway_config import chat_from_astrbot, load_gateway_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "provider": [
                {
                    "id": "Gemini/test",
                    "enable": True,
                    "provider_source_id": "Gemini",
                    "model": "test",
                }
            ],
            "provider_sources": [
                {
                    "id": "Gemini",
                    "type": "openai_chat_completion",
                    "api_base": "https://example.com/v1/",
                    "key": ["secret"],
                }
            ],
        }

    def test_resolves_model_source(self):
        item = chat_from_astrbot(self.config, "Gemini/test")
        self.assertEqual(item["url"], "https://example.com/v1/chat/completions")
        self.assertEqual(item["model"], "test")
        self.assertEqual(item["key"], "secret")

    def test_unsafe_base_urls_rejected(self):
        for base in (
            "http://example.com",
            "https://secret@example.com",
            "https://example.com?key=secret",
            "https://example.com#secret",
        ):
            self.config["provider_sources"][0]["api_base"] = base
            with self.subTest(base=base), self.assertRaises(ValueError):
                chat_from_astrbot(self.config, "Gemini/test")

    def test_missing_disabled_duplicate_or_non_chat_rejected(self):
        with self.assertRaises(ValueError):
            chat_from_astrbot(self.config, "other")
        self.config["provider"][0]["enable"] = False
        with self.assertRaises(ValueError):
            chat_from_astrbot(self.config, "Gemini/test")
        self.config["provider"][0]["enable"] = True
        self.config["provider_sources"][0]["type"] = "openai_responses"
        with self.assertRaises(ValueError):
            chat_from_astrbot(self.config, "Gemini/test")

    def test_credential_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gateway.json"
            path.write_text('{"routes": {}}')
            with patch.dict("os.environ", {"CREDENTIALS_DIRECTORY": tmp}):
                path.chmod(0o644)
                with self.assertRaises(ValueError):
                    load_gateway_config()
                path.chmod(0o600)
                self.assertEqual(load_gateway_config(), {"routes": {}})
                if path.stat().st_gid == 0:
                    path.chmod(0o440)
                    self.assertEqual(load_gateway_config(), {"routes": {}})
                path.chmod(0o620)
                with self.assertRaises(ValueError):
                    load_gateway_config()
