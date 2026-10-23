import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tenant_control.runtime import credentials, tenant_defaults


class RuntimeTests(unittest.TestCase):
    def test_private_credentials_and_conflicts(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(os.environ, {}, clear=True),
        ):
            root = Path(temp)
            path = (
                root
                / "data/plugin_data/astrbot_plugin_tenant_control/private/runtime.json"
            )
            path.parent.mkdir(parents=True)
            value = {
                "CONTROL_DB_PATH": str(root / "control.db"),
                "CONTROL_FERNET_KEY": "private-key",
                "CONTROL_ROTATED_TOKEN_ACK": "YES",
            }
            path.write_text(json.dumps(value))
            path.chmod(0o600)
            self.assertEqual(credentials(root), value)
            with patch.dict(os.environ, {"CONTROL_FERNET_KEY": "other-key"}):
                with self.assertRaisesRegex(ValueError, "Conflicting"):
                    credentials(root)
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "private"):
                credentials(root)

    def test_template_features_and_quota_are_validated(self):
        self.assertEqual(tenant_defaults({})["model_quota"], 1000)
        self.assertEqual(
            tenant_defaults({"default_features": [], "default_model_quota": 0})[
                "features"
            ],
            [],
        )
        for conf in (
            {"default_features": ["payments"]},
            {"default_model_quota": -1},
            {"default_model_quota": True},
            {"tenant_template_version": "arbitrary"},
        ):
            with self.assertRaises(ValueError):
                tenant_defaults(conf)
