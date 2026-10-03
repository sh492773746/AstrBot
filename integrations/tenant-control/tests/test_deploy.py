"""Installation preparation must not activate Telegram or reuse secrets."""

import json
import os
import runpy
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet
from tenant_control.bundle import build_plugin


class PrepareTests(unittest.TestCase):
    def test_isolated_disabled_install_and_repeat_preserves_credentials(self):
        deploy = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "deploy/prepare_vip.py")
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "instance"
            archive = build_plugin(Path(temp) / "plugin.zip")
            mask = os.umask(0o077)
            try:
                deploy["prepare"](root, archive)
            finally:
                os.umask(mask)
            config = json.loads((root / "data/cmd_config.json").read_text())
            platform = config["platform"][0]
            self.assertEqual(platform["id"], "VIP_DHBot")
            self.assertFalse(platform["enable"])
            self.assertEqual(platform["telegram_token"], "")
            self.assertIn("callback_query", platform["telegram_allowed_updates"])
            self.assertEqual(config["dashboard"]["host"], "127.0.0.1")
            self.assertFalse(config["provider_settings"]["enable"])
            self.assertEqual(config["admins_id"], [])
            plugin = root / "data/plugins/astrbot_plugin_tenant_control"
            self.assertTrue((plugin / "main.py").is_file())
            self.assertFalse((plugin / "tenant_control/worker.py").exists())
            environment = root / "private/controller.env"
            old = environment.read_bytes()
            values = dict(line.split("=", 1) for line in old.decode().splitlines())
            Fernet(values["CONTROL_FERNET_KEY"].encode())
            self.assertEqual(values["CONTROL_ROTATED_TOKEN_ACK"], "NO")
            self.assertEqual(environment.stat().st_mode & 0o777, 0o600)
            self.assertEqual((root / "private").stat().st_mode & 0o777, 0o700)
            with self.assertRaises(SystemExit):
                deploy["prepare"](root, archive)
            self.assertEqual(environment.read_bytes(), old)
