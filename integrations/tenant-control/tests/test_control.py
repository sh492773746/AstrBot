"""Control-plane tests run without Telegram, Docker, or production data."""

import asyncio
import grp
import hashlib
import json
import os
import tempfile
import time
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from cryptography.fernet import Fernet
from tenant_control.control import Control
from tenant_control.gateway import create_app, run_gateway
from tenant_control.store import Store, extend_epoch
from tenant_control.worker import Worker

REPO = Path(__file__).resolve().parents[3]
IMAGE = "astrbot@sha256:" + "a" * 64


class ControlFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "control.db")
        for tier, amount in (("first", 45), ("month", 50), ("year", 500)):
            self.store.set_price(tier, amount, 999)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _new_bot(self, owner=11, bot_id=123):
        order = self.store.create_order(owner, "first")
        self.store.settle(order["id"], owner, 45, f"charge-{bot_id}")
        self.store.attach_bot(owner, bot_id, "tenant_example_bot", b"encrypted")
        return order


class StoreTests(ControlFixture):
    def test_new_tenant_entitlements_are_snapshotted_and_rotation_keeps_them(self):
        defaults = {
            "template": "community-v1",
            "features": ["community"],
            "model_quota": 42,
        }
        order = self.store.create_order(11, "first")
        self.store.settle(order["id"], 11, 45, "snapshot-payment")
        self.store.attach_bot(11, 123, "tenant", b"cipher", defaults=defaults)
        changed = {"template": "community-v1", "features": [], "model_quota": 0}
        self.store.attach_bot(11, 123, "tenant", b"rotated", defaults=changed)
        one = self.store.db.execute(
            "SELECT * FROM tenant_entitlements WHERE bot_id=123"
        ).fetchone()
        self.assertEqual(one["model_quota"], 42)
        self.assertEqual(json.loads(one["features"]), ["community"])
        order = self.store.create_order(22, "first")
        self.store.settle(order["id"], 22, 45, "snapshot-payment-2")
        self.store.attach_bot(22, 456, "tenant2", b"cipher2", defaults=changed)
        two = self.store.db.execute(
            "SELECT * FROM tenant_entitlements WHERE bot_id=456"
        ).fetchone()
        self.assertEqual(two["model_quota"], 0)
        self.assertEqual(json.loads(two["features"]), [])

    def test_operation_confirmation_is_actor_bound_expiring_and_single_use(self):
        self._new_bot()
        ref = self.store.propose_operation(999, "pause", ["123"])
        with self.assertRaises(ValueError):
            self.store.confirm_operation(ref, 11)
        self.assertEqual(self.store.confirm_operation(ref, 999)["action"], "pause")
        with self.assertRaises(ValueError):
            self.store.confirm_operation(ref, 999)
        expired = self.store.propose_operation(999, "restore", ["123", "123456"])
        self.store.db.execute(
            "UPDATE operations SET created_at=0 WHERE id=?", (expired,)
        )
        with self.assertRaises(ValueError):
            self.store.confirm_operation(expired, 999)
        with self.assertRaises(ValueError):
            self.store.propose_operation(999, "restore", ["123", "../456"])
        with self.assertRaises(ValueError):
            self.store.propose_operation(999, "backup", ["456"])

    def test_shared_control_db_group_permissions(self):
        group = grp.getgrgid(os.getgid()).gr_name
        root = Path(self.tmp.name) / "shared"
        with patch.dict(os.environ, {"CONTROL_DB_GROUP": group}):
            other = Store(root / "control.db")
            try:
                self.assertEqual((root / "control.db").stat().st_mode & 0o777, 0o660)
                self.assertEqual(root.stat().st_mode & 0o777, 0o770)
                self.assertTrue(root.stat().st_mode & 0o2000)
            finally:
                other.close()

    def test_utc_calendar_month_and_leap_year(self):
        jan31 = int(datetime(2026, 1, 31, 12, tzinfo=timezone.utc).timestamp())
        self.assertEqual(
            datetime.fromtimestamp(extend_epoch(jan31, "month"), timezone.utc).day,
            28,
        )
        leap = int(datetime(2024, 2, 29, 12, tzinfo=timezone.utc).timestamp())
        self.assertEqual(
            datetime.fromtimestamp(extend_epoch(leap, "year"), timezone.utc).day,
            28,
        )

    def test_once_only_first_month_across_all_bots(self):
        self._new_bot()
        with self.assertRaisesRegex(ValueError, "offer"):
            self.store.create_order(11, "first")
        second = self.store.create_order(11, "month")
        self.assertEqual(second["bot_id"], None)
        self.store.settle(second["id"], 11, 50, "other-charge")
        created, bot_id = self.store.attach_bot(11, 456, "another_bot", b"secret")
        self.assertTrue(created)
        self.assertEqual(bot_id, 456)
        self.assertEqual(len(self.store.bots_for(11)), 2)

    def test_first_offer_reservation_and_year_purchase(self):
        self.store.create_order(11, "first")
        with self.assertRaisesRegex(ValueError, "reserved"):
            self.store.create_order(11, "first")
        order = self.store.create_order(22, "year")
        self.store.settle(order["id"], 22, 500, "annual")
        with self.assertRaisesRegex(ValueError, "offer"):
            self.store.create_order(22, "first")

    def test_checkout_owner_price_expiry_and_duplicate_receipt(self):
        order = self.store.create_order(11, "first")
        self.assertTrue(self.store.precheckout(order["id"], 11, 45))
        self.assertFalse(self.store.precheckout(order["id"], 12, 45))
        self.assertFalse(self.store.precheckout(order["id"], 11, 46))
        with self.assertRaisesRegex(ValueError, "match"):
            self.store.settle(order["id"], 12, 45, "charge")
        self.store.settle(order["id"], 11, 45, "charge")
        self.assertEqual(
            self.store.settle(order["id"], 11, 45, "charge")["status"], "paid"
        )
        with self.assertRaisesRegex(ValueError, "another"):
            self.store.settle(order["id"], 11, 45, "wrong-charge")
        with self.assertRaisesRegex(ValueError, "No paid"):
            self.store.attach_bot(12, 333, "wrong_bot", b"cipher")

    def test_renewal_extends_existing_expiry_and_isolated_ownership(self):
        self._new_bot()
        original = self.store.bots_for(11)[0]["expires_at"]
        with self.assertRaisesRegex(ValueError, "belong"):
            self.store.create_order(12, "month", 123)
        renewal = self.store.create_order(11, "month", 123)
        self.store.settle(renewal["id"], 11, 50, "renewal")
        self.assertEqual(
            self.store.bots_for(11)[0]["expires_at"], extend_epoch(original, "month")
        )
        self.assertEqual(
            self.store.settle(renewal["id"], 11, 50, "renewal")["status"], "active"
        )

    def test_refunded_unfulfilled_order_frees_first_eligibility(self):
        order = self.store.create_order(11, "first")
        self.store.settle(order["id"], 11, 45, "refund-me")
        self.store.refundable(order["id"])
        self.store.refund(order["id"], 999)
        self.assertEqual(self.store.create_order(11, "first")["tier"], "first")

    def test_refund_cannot_erase_another_paid_subscription(self):
        order = self._new_bot()
        renewal = self.store.create_order(11, "month", 123)
        self.store.settle(renewal["id"], 11, 50, "renewed")
        with self.assertRaisesRegex(ValueError, "other entitlements"):
            self.store.refund(order["id"], 999)
        self.assertGreater(self.store.bots_for(11)[0]["expires_at"], int(time.time()))

    def test_custom_quote_scoped_to_owned_bot(self):
        self._new_bot()
        with self.assertRaisesRegex(ValueError, "Unknown"):
            self.store.create_quote(12, 123, 100, "Custom feature", 999)
        quote = self.store.create_quote(11, 123, 100, "Custom feature", 999)
        self.assertTrue(self.store.precheckout(quote["id"], 11, 100))
        self.store.settle(quote["id"], 11, 100, "custom-charge")
        self.assertEqual(
            self.store.db.execute(
                "SELECT status FROM orders WHERE id=?", (quote["id"],)
            ).fetchone()[0],
            "paid",
        )

    def test_worker_heartbeat_gates_checkout(self):
        self.assertFalse(self.store.worker_ready())
        self.store.db.execute(
            "INSERT INTO settings VALUES('worker_heartbeat',?)",
            (str(int(time.time())),),
        )
        self.assertTrue(self.store.worker_ready())

    def test_admin_pause_persists_without_consuming_subscription(self):
        self._new_bot()
        old_expiry = self.store.bots_for(11)[0]["expires_at"]
        self.store.set_bot_enabled(123, False, 999)
        self.assertEqual(
            self.store.db.execute("SELECT enabled FROM bots WHERE id=123").fetchone()[
                0
            ],
            0,
        )
        self.store.set_bot_enabled(123, True, 999)
        self.assertEqual(self.store.bots_for(11)[0]["expires_at"], old_expiry)


class WorkerTests(ControlFixture):
    def test_reconcile_accepts_astrbot_utf8_bom_configuration(self):
        self._paid_bot(11, 123, "token-one")
        self.worker.reconcile(123)
        path = self.root / "123/data/cmd_config.json"
        text = path.read_text()
        path.write_text(text, encoding="utf-8-sig")
        self.worker.reconcile(123)
        self.assertFalse(json.loads(path.read_text())["dashboard"]["enable"])

    def test_resume_waits_for_new_health_before_marking_operation_done(self):
        self._paid_bot(11, 123, "token-one")
        self.worker.reconcile(123)
        self.worker._state = lambda bot_id: "stopped"
        self.worker._healthy = unittest.mock.Mock(side_effect=[False, False, True])
        ref = self.store.propose_operation(999, "resume", ["123"])
        self.store.confirm_operation(ref, 999)
        with patch("tenant_control.worker.time.sleep"):
            self.assertTrue(self.worker.run_operation())
        row = self.store.db.execute(
            "SELECT state FROM operations WHERE id=?", (ref,)
        ).fetchone()
        self.assertEqual(row["state"], "done")
        self.assertEqual(self.worker._healthy.call_count, 3)

    def test_chat_only_template_needs_no_embedding_and_has_no_web(self):
        path = Path(self.tmp.name) / "chat-only.json"
        path.write_text(
            json.dumps(
                {
                    "provider": [
                        {
                            "id": "chat",
                            "type": "openai_chat_completion",
                            "key": [],
                            "model": "gemini-3.8-flash",
                        }
                    ],
                    "chat_provider_id": "chat",
                }
            )
        )
        with patch.dict(
            os.environ,
            {
                "TENANT_PROVIDER_PROXY_URL": "http://host.docker.internal:18734/v1",
            },
        ):
            worker = Worker(self.store, self.fernet, self.root, REPO, IMAGE, path)
        self._new_bot()
        self.store.db.execute(
            "UPDATE bots SET token_cipher=? WHERE id=123",
            (self.fernet.encrypt(b"token"),),
        )
        data, _ = worker._prepare(
            self.store.db.execute("SELECT * FROM bots WHERE id=123").fetchone()
        )
        config = json.loads((data / "cmd_config.json").read_text())
        plugin = json.loads(
            (data / "config/astrbot_plugin_superbot_config.json").read_text()
        )
        self.assertEqual(len(config["provider"]), 1)
        self.assertFalse(config["dashboard"]["enable"])
        self.assertTrue(plugin["chat_only_tenant"])
        self.assertEqual(plugin["embedding_provider_id"], "")

    def test_local_content_addressed_image_is_allowed_but_tags_are_not(self):
        with patch.dict(
            os.environ,
            {"TENANT_PROVIDER_PROXY_URL": "http://host.docker.internal:18734/v1"},
        ):
            worker = Worker(
                self.store,
                self.fernet,
                self.root,
                REPO,
                "sha256:" + "a" * 64,
                Path(self.tmp.name) / "provider.json",
            )
            self.assertEqual(worker.image, "sha256:" + "a" * 64)
            with self.assertRaises(ValueError):
                Worker(
                    self.store,
                    self.fernet,
                    self.root,
                    REPO,
                    "astrbot:latest",
                    Path(self.tmp.name) / "provider.json",
                )

    def setUp(self):
        super().setUp()
        template = Path(self.tmp.name) / "provider.json"
        template.write_text(
            json.dumps(
                {
                    "provider": [
                        {"id": "chat", "type": "openai_chat_completion", "key": []},
                        {
                            "id": "embedding",
                            "type": "openai_embedding",
                            "embedding_api_key": "",
                        },
                    ],
                    "chat_provider_id": "chat",
                    "embedding_provider_id": "embedding",
                }
            )
        )
        self.fernet = Fernet(Fernet.generate_key())
        self.root = Path(self.tmp.name) / "tenants"
        with patch.dict(
            os.environ,
            {"TENANT_PROVIDER_PROXY_URL": "http://host.docker.internal:18734/v1"},
        ):
            self.worker = Worker(
                self.store, self.fernet, self.root, REPO, IMAGE, template
            )
        self.commands = []

        def docker(*args):
            self.commands.append(args)
            return "container-id"

        self.worker._docker = docker
        self.worker._state = lambda bot_id: "missing"
        self.worker.proxy_ready = lambda: True
        self.worker._healthy = lambda bot_id: True
        self.health = patch(
            "tenant_control.worker.urllib.request.urlopen",
            return_value=nullcontext(),
        )
        self.health.start()
        self.addCleanup(self.health.stop)

    def _paid_bot(self, owner: int, bot_id: int, token: str):
        order = self.store.create_order(owner, "first")
        self.store.settle(order["id"], owner, 45, f"charge-{bot_id}")
        self.store.attach_bot(
            owner, bot_id, f"bot{bot_id}", self.fernet.encrypt(token.encode())
        )

    def test_two_containers_have_no_shared_data_or_credentials(self):
        self._paid_bot(11, 123, "token-one")
        self._paid_bot(22, 456, "token-two")
        self.worker.reconcile(123)
        self.worker.reconcile(456)
        one = self.root / "123/data"
        two = self.root / "456/data"
        config_one = json.loads((one / "cmd_config.json").read_text())
        config_two = json.loads((two / "cmd_config.json").read_text())
        self.assertEqual(config_one["platform"][0]["telegram_token"], "token-one")
        self.assertEqual(config_two["platform"][0]["telegram_token"], "token-two")
        self.assertNotIn("token-two", (one / "cmd_config.json").read_text())
        self.assertNotEqual(
            config_one["provider"][0]["key"],
            config_two["provider"][0]["key"],
        )
        self.assertEqual(
            config_one["provider"][0]["api_base"],
            "http://host.docker.internal:18734/v1",
        )
        self.assertFalse(config_one["dashboard"]["enable"])
        self.assertFalse(config_two["dashboard"]["enable"])
        self.assertEqual(config_one["dashboard"]["password"], "")
        self.assertEqual(config_one["admins_id"], ["11"])
        self.assertEqual(config_two["admins_id"], ["22"])
        self.assertIsNone(
            self.store.db.execute(
                "SELECT dashboard_secret FROM bots WHERE id=123"
            ).fetchone()[0]
        )
        self.assertEqual(
            json.loads(
                (one / "config/astrbot_plugin_superbot_config.json").read_text()
            )["owner_uid"],
            "11",
        )
        self.assertIsNone(self.store.bots_for(11)[0]["dashboard_port"])
        self.assertIsNone(self.store.bots_for(22)[0]["dashboard_port"])
        self.assertTrue(
            (one / "plugins/astrbot_plugin_tenant_health/main.py").is_file()
        )
        self.assertFalse(any("-p" in cmd for cmd in self.commands))
        self.assertFalse((one / "data_v4.db").exists())
        self.assertTrue(any(cmd[0] == "run" for cmd in self.commands))

    def test_worker_runs_confirmed_backup_once_and_audits_result(self):
        self._paid_bot(11, 123, "token-one")
        self.worker.reconcile(123)
        ref = self.store.propose_operation(999, "backup", ["123"])
        self.assertFalse(self.worker.run_operation())
        self.store.confirm_operation(ref, 999)
        self.assertTrue(self.worker.run_operation())
        self.assertFalse(self.worker.run_operation())
        row = self.store.db.execute(
            "SELECT * FROM operations WHERE id=?", (ref,)
        ).fetchone()
        self.assertEqual(row["state"], "done")
        self.assertTrue(row["result"].isdigit())
        self.assertTrue((self.root / "123/backups" / row["result"]).is_dir())
        self.assertEqual(
            self.store.db.execute(
                "SELECT COUNT(*) FROM audit WHERE action='backup_done' AND ref=?",
                (ref,),
            ).fetchone()[0],
            1,
        )

    def test_stale_or_wrong_tenant_heartbeat_is_not_healthy(self):
        for report, expected in (
            ({"platform_id": "tenant-123", "ready": True, "at": time.time()}, True),
            ({"platform_id": "tenant-456", "ready": True, "at": time.time()}, False),
            ({"platform_id": "tenant-123", "ready": True, "at": 0}, False),
            ({"platform_id": "tenant-123", "ready": False, "at": time.time()}, False),
        ):
            with patch.object(self.worker, "_docker", return_value=json.dumps(report)):
                self.assertEqual(Worker._healthy(self.worker, 123), expected)

    def test_failed_operation_requires_review_without_automatic_replay(self):
        self._paid_bot(11, 123, "token-one")
        ref = self.store.propose_operation(999, "restore", ["123", "123456"])
        self.store.confirm_operation(ref, 999)
        self.assertTrue(self.worker.run_operation())
        self.assertFalse(self.worker.run_operation())
        self.assertEqual(
            self.store.db.execute(
                "SELECT state FROM operations WHERE id=?", (ref,)
            ).fetchone()[0],
            "needs_review",
        )

    def test_suspend_on_expiry_and_resume_without_clearing_data(self):
        self._paid_bot(11, 123, "token-one")
        self.worker.reconcile(123)
        self.store.db.execute("UPDATE bots SET expires_at=0 WHERE id=123")
        self.worker._state = lambda bot_id: "running"
        self.worker.reconcile(123)
        self.assertEqual(self.store.bots_for(11)[0]["status"], "archived")
        self.assertTrue(any(cmd[0] == "stop" for cmd in self.commands))
        self.store.db.execute(
            "UPDATE bots SET expires_at=?,status='archived' WHERE id=123",
            (int(time.time()) + 86400,),
        )
        self.worker._state = lambda bot_id: "stopped"
        self.worker.reconcile(123)
        self.assertEqual(self.store.bots_for(11)[0]["status"], "active")
        self.assertTrue(any(cmd[0] == "start" for cmd in self.commands))
        self.assertTrue((self.root / "123/data/cmd_config.json").exists())

    def test_operator_pause_stops_active_instance(self):
        self._paid_bot(11, 123, "token-one")
        self.worker.reconcile(123)
        self.worker._state = lambda bot_id: "running"
        self.store.set_bot_enabled(123, False, 999)
        self.worker.reconcile(123)
        self.assertEqual(self.store.bots_for(11)[0]["status"], "paused")
        self.assertTrue(any(cmd[0] == "stop" for cmd in self.commands))

    def test_backup_and_paid_extension_are_tenant_scoped(self):
        import zipfile

        self._paid_bot(11, 123, "token-one")
        self.worker.reconcile(123)
        quote = self.store.create_quote(11, 123, 100, "feature", 999)
        self.store.settle(quote["id"], 11, 100, "extension-charge")
        archive = Path(self.tmp.name) / "addon.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("metadata.yaml", "name: custom")
            output.writestr("main.py", "# plugin")
        with self.assertRaisesRegex(ValueError, "Approved"):
            self.worker.install_extension(123, quote["id"], archive)
        staged = self.worker.stage_extension(123, quote["id"], archive)
        self.assertEqual(
            json.loads((staged / "cmd_config.json").read_text())["platform"], []
        )
        self.assertEqual(
            json.loads((staged / "cmd_config.json").read_text())["provider"], []
        )
        self.assertEqual(
            json.loads(
                (staged / "config/astrbot_plugin_superbot_config.json").read_text()
            )["enabled"],
            False,
        )
        changed = Path(self.tmp.name) / "changed.zip"
        with zipfile.ZipFile(changed, "w") as output:
            output.writestr("metadata.yaml", "name: other")
            output.writestr("main.py", "# changed")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.worker.approve_extension(123, quote["id"], changed)
        self.worker.approve_extension(123, quote["id"], archive)
        with self.assertRaisesRegex(ValueError, "Approved"):
            self.worker.install_extension(123, quote["id"], changed)
        self.worker.install_extension(123, quote["id"], archive)
        self.assertTrue(
            (self.root / "123/data/plugins/astrbot_plugin_custom_123/main.py").exists()
        )
        with self.assertRaisesRegex(ValueError, "Paid"):
            self.worker.install_extension(123, "other-order", archive)
        self.assertEqual(
            self.store.db.execute(
                "SELECT status FROM orders WHERE id=?", (quote["id"],)
            ).fetchone()[0],
            "active",
        )

    def test_extension_upgrade_backup_and_restore(self):
        import zipfile

        self._paid_bot(11, 123, "token-one")
        self.worker.reconcile(123)
        previous = self.store.create_quote(11, 123, 100, "Initial feature", 999)
        self.store.settle(previous["id"], 11, 100, "custom-one")
        archive = Path(self.tmp.name) / "addon.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("metadata.yaml", "name: custom")
            output.writestr("main.py", "# old version")
        self.worker.stage_extension(123, previous["id"], archive)
        self.worker.approve_extension(123, previous["id"], archive)
        self.worker.install_extension(123, previous["id"], archive)
        upgrade = self.store.create_quote(11, 123, 100, "Upgrade", 999)
        self.store.settle(upgrade["id"], 11, 100, "custom-two")
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("metadata.yaml", "name: custom")
            output.writestr("main.py", "# new version")
        self.worker.stage_extension(123, upgrade["id"], archive)
        self.worker.approve_extension(123, upgrade["id"], archive)
        self.worker.install_extension(123, upgrade["id"], archive)
        snapshots = sorted((self.root / "123/backups").iterdir())
        self.assertGreaterEqual(len(snapshots), 2)
        plugin = self.root / "123/data/plugins/astrbot_plugin_custom_123/main.py"
        self.assertEqual(plugin.read_text(), "# new version")
        self.worker.restore(123, snapshots[-1].name)
        self.assertEqual(plugin.read_text(), "# old version")
        with self.assertRaisesRegex(ValueError, "numeric"):
            self.worker.restore(123, "../production")

    def test_unsafe_extension_zip_rejected_without_writing_outside_tenant(self):
        import zipfile

        self._paid_bot(11, 123, "token-one")
        self.worker.reconcile(123)
        quote = self.store.create_quote(11, 123, 100, "feature", 999)
        self.store.settle(quote["id"], 11, 100, "custom")
        archive = Path(self.tmp.name) / "unsafe.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("metadata.yaml", "name: custom")
            output.writestr("main.py", "# plugin")
            output.writestr("../outside", "do not write")
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            self.worker.stage_extension(123, quote["id"], archive)
        self.assertFalse((self.root / "123/outside").exists())

    def test_gateway_down_blocks_new_instance_but_allows_expiry_stop(self):
        self._paid_bot(11, 123, "token-one")
        self.worker.proxy_ready = lambda: False
        with self.assertRaisesRegex(ValueError, "gateway"):
            self.worker.reconcile(123)
        self.assertFalse((self.root / "123/data").exists())
        self.store.db.execute("UPDATE bots SET expires_at=0 WHERE id=123")
        self.worker._state = lambda bot_id: "running"
        self.worker.reconcile(123)
        self.assertTrue(any(command[0] == "stop" for command in self.commands))

    def test_base_upgrade_checks_extensions_and_restores_snapshot(self):
        self._paid_bot(11, 123, "token-one")
        self.worker.reconcile(123)
        plugin = self.root / "123/data/plugins/astrbot_plugin_superbot"
        original = plugin / "main.py"
        original.write_text("# previous version")
        (self.root / "123/data/plugins/astrbot_plugin_custom_123").mkdir()
        with self.assertRaisesRegex(ValueError, "compatibility"):
            self.worker.upgrade_plugin(123)
        snapshot = self.worker.upgrade_plugin(123, custom_approved=True)
        self.assertNotEqual(original.read_text(), "# previous version")
        self.worker.restore(123, snapshot.name)
        self.assertEqual(original.read_text(), "# previous version")
        self.assertFalse((self.root / "123/data/previous-base-plugin").exists())


class ControlTests(ControlFixture):
    def setUp(self):
        super().setUp()
        self.fernet = Fernet(Fernet.generate_key())
        self.control = Control(self.store, self.fernet, {999})
        self.control.purchase_allowed = True

    def test_managed_update_only_binds_paid_order_and_owner(self):
        bot_api = SimpleNamespace(
            get_managed_bot_token=AsyncMock(return_value="real-token"),
            send_message=AsyncMock(),
        )
        update = SimpleNamespace(
            managed_bot=SimpleNamespace(
                user=SimpleNamespace(id=11),
                bot=SimpleNamespace(id=123, username="tenant_bot"),
            )
        )
        asyncio.run(self.control.managed(update, SimpleNamespace(bot=bot_api)))
        self.assertEqual(len(self.store.bots_for(11)), 0)
        bot_api.get_managed_bot_token.assert_not_awaited()
        order = self.store.create_order(11, "first")
        self.store.settle(order["id"], 11, 45, "charge")
        asyncio.run(self.control.managed(update, SimpleNamespace(bot=bot_api)))
        self.assertEqual(len(self.store.bots_for(11)), 1)
        self.assertEqual(
            self.fernet.decrypt(
                self.store.db.execute(
                    "SELECT token_cipher FROM bots WHERE id=123"
                ).fetchone()[0]
            ).decode(),
            "real-token",
        )
        bot_api.get_managed_bot_token.assert_awaited_once_with(123)

    def test_checkout_requires_fresh_worker_but_no_web_dashboard(self):
        self.store.db.execute(
            "INSERT INTO settings VALUES('worker_heartbeat',?)",
            (str(int(time.time())),),
        )
        order = self.store.create_order(11, "first")
        query = SimpleNamespace(
            currency="XTR",
            invoice_payload=order["id"],
            from_user=SimpleNamespace(id=11),
            total_amount=45,
            answer=AsyncMock(),
        )
        update = SimpleNamespace(pre_checkout_query=query)
        with patch.dict(os.environ, {}, clear=True):
            asyncio.run(self.control.checkout(update, SimpleNamespace()))
        query.answer.assert_awaited_with(ok=True, error_message=None)
        self.store.db.execute("DELETE FROM settings WHERE key='worker_heartbeat'")
        with patch.dict(os.environ, {}, clear=True):
            asyncio.run(self.control.checkout(update, SimpleNamespace()))
        query.answer.assert_awaited_with(ok=False, error_message=unittest.mock.ANY)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "control.db")
        self.store.db.execute(
            "INSERT INTO bots(id,owner_id,username,token_cipher,expires_at,status,"
            "provider_key_hash) VALUES(1,11,'bot',X'01',?,'active',?)",
            (int(time.time()) + 3600, hashlib.sha256(b"tenant-key").hexdigest()),
        )
        self.server = TestServer(
            create_app(
                self.store,
                {
                    "/v1/chat/completions": (
                        "https://chat.example/v1/chat/completions",
                        "master",
                    ),
                    "/v1/embeddings": ("https://embed.example/v1/embeddings", "master"),
                },
                2,
                {
                    "/v1/chat/completions": "approved-chat",
                    "/v1/embeddings": "approved-embed",
                },
            )
        )
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self.tmp.cleanup()

    async def test_auth_model_override_and_quota(self):
        route = "/v1/chat/completions"
        payload = {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "forbidden",
        }
        self.assertEqual((await self.client.post(route, json=payload)).status, 401)
        self.assertEqual(
            (
                await self.client.post(
                    route, json=payload, headers={"Authorization": "Bearer invalid"}
                )
            ).status,
            403,
        )

        class Response:
            status = 200

            @property
            def content(self):
                return self

            async def iter_chunked(self, size):
                yield b'{"choices":[]}'

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def read(self):
                return b'{"choices":[]}'

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            def post(self, url, *, json, headers, allow_redirects):
                if allow_redirects:
                    raise AssertionError("Gateway must not follow redirects")
                self_call = (url, json, headers)
                calls.append(self_call)
                return Response()

        calls = []
        headers = {"Authorization": "Bearer tenant-key"}
        with patch("tenant_control.gateway.ClientSession", return_value=Session()):
            self.assertEqual(
                (await self.client.post(route, json=payload, headers=headers)).status,
                200,
            )
            self.assertEqual(
                (await self.client.post(route, json=payload, headers=headers)).status,
                200,
            )
            self.assertEqual(
                (await self.client.post(route, json=payload, headers=headers)).status,
                429,
            )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1]["model"], "approved-chat")
        self.assertEqual(calls[0][1]["max_tokens"], 512)
        self.assertEqual(calls[0][2], {"Authorization": "Bearer master"})
        self.assertNotIn("master", str(payload))

    async def test_zero_tenant_quota_overrides_global_allowance(self):
        self.store.db.execute(
            "INSERT INTO tenant_entitlements VALUES(1,'community-v1','[]',0)"
        )
        response = await self.client.post(
            "/v1/embeddings",
            json={"input": "hello"},
            headers={"Authorization": "Bearer tenant-key"},
        )
        self.assertEqual(response.status, 429)

    async def test_expired_tenant_and_oversized_request_rejected(self):
        self.store.db.execute("UPDATE bots SET expires_at=0 WHERE id=1")
        response = await self.client.post(
            "/v1/embeddings",
            json={"input": "hello"},
            headers={"Authorization": "Bearer tenant-key"},
        )
        self.assertEqual(response.status, 403)
        self.store.db.execute(
            "UPDATE bots SET expires_at=? WHERE id=1", (int(time.time()) + 3600,)
        )
        response = await self.client.post(
            "/v1/chat/completions",
            json={"messages": [], "input": "x" * 17000},
            headers={"Authorization": "Bearer tenant-key"},
        )
        self.assertEqual(response.status, 413)

    async def test_public_gateway_bind_is_rejected(self):
        with patch.dict(os.environ, {"TENANT_GATEWAY_BIND": "203.0.113.2"}):
            with self.assertRaisesRegex(ValueError, "private Docker bridge"):
                run_gateway(Path(self.tmp.name) / "unused.db")
        self.assertFalse((Path(self.tmp.name) / "unused.db").exists())


if __name__ == "__main__":
    unittest.main()
