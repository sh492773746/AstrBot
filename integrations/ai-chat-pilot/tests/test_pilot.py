"""Exercise the pilot without connecting to Telegram or an AI provider."""

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from chat_pilot.__main__ import Pilot, draft_reply


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pilot = Pilot(Path(self.tmp.name) / "pilot.db")
        self.pilot.add_account("operator-a")
        self.pilot.add_account("operator-b")
        with patch("chat_pilot.__main__.time.time", return_value=0):
            self.pilot.subscribe("tenant-a", 5000)
            self.pilot.subscribe("tenant-b", 5000)

    def tearDown(self):
        self.pilot.db.close()
        self.tmp.cleanup()

    def test_exclusive_assignment_and_idempotence(self):
        self.assertEqual(self.pilot.allocate("tenant-a", 1000), "operator-a")
        self.assertEqual(self.pilot.allocate("tenant-a", 1000), "operator-a")
        self.assertEqual(self.pilot.allocate("tenant-b", 1000), "operator-b")
        with patch("chat_pilot.__main__.time.time", return_value=0):
            self.pilot.subscribe("tenant-c", 5000)
        with self.assertRaisesRegex(ValueError, "No account"):
            self.pilot.allocate("tenant-c", 1000)

    def test_inactive_tenant_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "inactive"):
            self.pilot.allocate("tenant-a", 5000)
        self.pilot.allocate("tenant-a", 1000)
        self.pilot.pause("tenant", "tenant-a")
        with self.assertRaisesRegex(ValueError, "inactive"):
            self.pilot.allocate("tenant-a", 1000)

    def test_account_pause_blocks_drafts(self):
        self.pilot.allocate("tenant-a", 1000)
        self.pilot.allow_group("tenant-a", -10099)
        self.pilot.pause("account", "operator-a")
        with self.assertRaisesRegex(ValueError, "paused"):
            self.pilot.allocate("tenant-a", 1000)
        with self.assertRaisesRegex(ValueError, "Inactive"):
            self.pilot.plan(
                "tenant-a",
                {
                    "group_id": -10099,
                    "message_id": 1,
                    "sender_id": 42,
                    "sender_is_bot": False,
                    "sender_is_self": False,
                    "text": "hello",
                },
                1000,
            )

    def test_only_allowed_incoming_human_messages_and_cooldown(self):
        self.pilot.allocate("tenant-a", 1000)
        self.pilot.allow_group("tenant-a", -10099)
        event = {
            "group_id": -10099,
            "message_id": 1,
            "sender_id": 42,
            "text": "How are things?",
            "sender_is_bot": False,
            "sender_is_self": False,
        }
        self.assertEqual(self.pilot.plan("tenant-a", event, 1000), "operator-a")
        with self.assertRaisesRegex(ValueError, "already processed"):
            self.pilot.plan("tenant-a", event, 1200)
        with self.assertRaisesRegex(ValueError, "cooldown"):
            self.pilot.plan("tenant-a", {**event, "message_id": 2}, 1119)
        self.assertEqual(
            self.pilot.plan("tenant-a", {**event, "message_id": 2}, 1120),
            "operator-a",
        )
        for change in (
            {"group_id": -10100},
            {"sender_is_bot": True},
            {"sender_is_self": True},
            {"text": ""},
        ):
            with self.assertRaises(ValueError):
                self.pilot.plan(
                    "tenant-a", {**event, **change, "message_id": 3}, 1300
                )
        with self.assertRaisesRegex(ValueError, "unapproved"):
            self.pilot.plan("tenant-b", {**event, "message_id": 3}, 1300)

    def test_expiry_blocks_drafts(self):
        self.pilot.allocate("tenant-a", 1000)
        self.pilot.allow_group("tenant-a", -10099)
        with self.assertRaisesRegex(ValueError, "Inactive"):
            self.pilot.plan(
                "tenant-a",
                {
                    "group_id": -10099,
                    "message_id": 1,
                    "sender_id": 42,
                    "sender_is_bot": False,
                    "sender_is_self": False,
                    "text": "hello",
                },
                5000,
            )

    def test_provider_requires_explicit_settings(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                draft_reply("hello")

    def test_cli_allocation_and_dry_run_without_model(self):
        database = Path(self.tmp.name) / "cli.db"

        def run(*args, input_text=None):
            return subprocess.run(
                [sys.executable, "-m", "chat_pilot", "--db", str(database), *args],
                input=input_text,
                text=True,
                capture_output=True,
                check=False,
                env={"PATH": "/usr/bin:/bin"},
            )

        self.assertEqual(run("account", "example-account").returncode, 0)
        self.assertEqual(
            run("subscribe", "example-tenant", str(int(time.time()) + 3600)).returncode,
            0,
        )
        self.assertEqual(
            run("allocate", "example-tenant").stdout.strip(), "example-account"
        )
        self.assertEqual(run("group", "example-tenant", "-10099").returncode, 0)
        event = {
            "group_id": -10099,
            "message_id": 1,
            "sender_id": 42,
            "text": "hello",
            "sender_is_bot": False,
            "sender_is_self": False,
        }
        result = run("draft", "example-tenant", input_text=json.dumps(event))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Configure an HTTPS model endpoint", result.stderr)
        self.assertEqual(run("pause", "tenant", "example-tenant").returncode, 0)


if __name__ == "__main__":
    unittest.main()
