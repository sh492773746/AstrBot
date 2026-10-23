"""No-payment trials are confirmed, bounded and isolated from billing."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
from tenant_control.control import Control
from tenant_control.store import Store


class TrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "control.db")
        self.control = Control(self.store, Fernet(Fernet.generate_key()), {999})
        self.control.trials_allowed = True

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def grant(self, owner=11):
        ref = self.store.propose_operation(999, "trial", [str(owner), "2"])
        self.store.confirm_operation(ref, 999)
        return ref

    def test_confirmation_is_atomic_actor_bound_expiring_single_use(self):
        ref = self.store.propose_operation(999, "trial", ["11", "2"])
        self.assertIsNone(self.store.pending_trial(11))
        with self.assertRaises(ValueError):
            self.store.confirm_operation(ref, 11)
        self.store.confirm_operation(ref, 999)
        with self.assertRaises(ValueError):
            self.store.confirm_operation(ref, 999)
        self.assertIsNotNone(self.store.pending_trial(11))
        self.assertIsNone(self.store.pending_trial(22))
        duplicate = self.store.propose_operation(999, "trial", ["11", "3"])
        with self.assertRaises(ValueError):
            self.store.confirm_operation(duplicate, 999)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM trials").fetchone()[0], 1
        )
        expired = self.store.propose_operation(999, "trial", ["22", "1"])
        self.store.db.execute(
            "UPDATE operations SET created_at=0 WHERE id=?", (expired,)
        )
        with self.assertRaises(ValueError):
            self.store.confirm_operation(expired, 999)
        self.assertIsNone(self.store.pending_trial(22))

    def test_input_limits(self):
        for args in (
            ["0", "1"],
            ["11", "0"],
            ["11", "8"],
            ["-1", "2"],
            ["11"],
            ["11", "2", "3"],
            ["a", "2"],
            [str(2**60), "1"],
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.store.propose_operation(999, "trial", args)

    def test_binding_is_single_use_keeps_expiry_and_no_payments(self):
        ref = self.grant()
        expiry = self.store.pending_trial(11)["expires_at"]
        with self.assertRaises(ValueError):
            self.store.attach_bot(11, 123, "test", b"cipher")
        with self.assertRaises(ValueError):
            self.store.attach_bot(22, 123, "test", b"cipher", allow_trial=True)
        self.store.attach_bot(11, 123, "test", b"cipher", allow_trial=True)
        self.assertIsNone(self.store.pending_trial(11))
        self.assertEqual(self.store.bots_for(11)[0]["expires_at"], expiry)
        self.store.attach_bot(11, 123, "test", b"new", allow_trial=True)
        with self.assertRaises(ValueError):
            self.store.attach_bot(11, 456, "test2", b"cipher", allow_trial=True)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM orders").fetchone()[0], 0
        )
        self.assertEqual(
            self.store.db.execute(
                "SELECT state FROM operations WHERE id=?", (ref,)
            ).fetchone()[0],
            "done",
        )
        self.store.set_price("first", 10, 999)
        self.assertEqual(self.store.create_order(11, "first")["tier"], "first")

    def test_expired_grant_cannot_bind(self):
        self.grant()
        self.store.db.execute("UPDATE trials SET expires_at=0")
        with self.assertRaises(ValueError):
            self.store.attach_bot(11, 123, "test", b"cipher", allow_trial=True)

    def test_private_admin_and_feature_gate_checked_again_on_confirmation(self):
        reply = AsyncMock()
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=11),
            message=SimpleNamespace(reply_text=reply),
        )
        context = SimpleNamespace(args=["11", "2"])
        asyncio.run(self.control.admin_trial(update, context))
        reply.assert_not_awaited()
        update.effective_user.id = 999
        update.effective_chat.type = "group"
        asyncio.run(self.control.admin_trial(update, context))
        reply.assert_not_awaited()
        update.effective_chat.type = "private"
        asyncio.run(self.control.admin_trial(update, context))
        ref = self.store.db.execute("SELECT id FROM operations").fetchone()[0]
        self.control.trials_allowed = False
        asyncio.run(self.control.admin_confirm(update, SimpleNamespace(args=[ref])))
        self.assertIsNone(self.store.pending_trial(11))
        self.control.trials_allowed = True
        asyncio.run(self.control.admin_confirm(update, SimpleNamespace(args=[ref])))
        self.assertIsNotNone(self.store.pending_trial(11))

    def test_managed_creation_requires_feature_and_worker_readiness(self):
        self.grant()
        bot = SimpleNamespace(
            get_managed_bot_token=AsyncMock(return_value="token"),
            send_message=AsyncMock(),
        )
        update = SimpleNamespace(
            managed_bot=SimpleNamespace(
                user=SimpleNamespace(id=11),
                bot=SimpleNamespace(id=123, username="test"),
            )
        )
        context = SimpleNamespace(bot=bot)
        asyncio.run(self.control.managed(update, context))
        bot.get_managed_bot_token.assert_not_awaited()
        self.store.db.execute(
            "INSERT INTO settings VALUES('worker_heartbeat',?)",
            (str(int(time.time())),),
        )
        self.control.trials_allowed = False
        asyncio.run(self.control.managed(update, context))
        bot.get_managed_bot_token.assert_not_awaited()
        self.control.trials_allowed = True
        asyncio.run(self.control.managed(update, context))
        bot.get_managed_bot_token.assert_awaited_once()
        self.assertEqual(len(self.store.bots_for(11)), 1)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM orders").fetchone()[0], 0
        )
