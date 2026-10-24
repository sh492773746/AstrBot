"""Explicit, fixed-account financial rehearsal using a separate SQLite ledger."""

import time
import uuid

from .cashback import CashbackService
from .cashback_store import CashbackError

TEST_GROUP = "1459282"
TEST_ACCOUNT = "43506527"
TEST_MANAGER = "31846829"


class FinanceSandboxService(CashbackService):
    """Use labeled synthetic invitations, never real financial records."""

    async def _scan(self, group, **_kwargs):
        if group != TEST_GROUP:
            raise CashbackError("test_scope", "测试仅限指定测试群。", 403)
        self.store.snapshot(
            group,
            self.collector_id,
            TEST_ACCOUNT,
            self.test_members,
            self.test_evidence,
            now=time.time(),
        )
        return self.test_members

    async def roster(self, adapter, group):
        if group != TEST_GROUP or str(adapter.account) != TEST_ACCOUNT:
            raise CashbackError("test_scope", "测试账号或群不匹配。", 403)
        return self.test_members

    def historical_members(self, group):
        if group != TEST_GROUP:
            raise CashbackError("test_scope", "测试群不匹配。", 403)
        return set()

    def seed(self, manager_peer, name):
        """Build a fresh labeled rehearsal from a verified manager.

        Args:
            manager_peer: Platform-directory verified manager transport identity.
            name: Platform-directory verified manager display name.
        """
        owner = "finance-test@example.invalid"
        self.store.bootstrap([{"email": owner, "name": "测试账本管理员"}])
        now = time.time()
        joined = now - 10860
        self.test_members = {
            TEST_MANAGER: {"name": name, "peer": manager_peer, "eligible": True}
        }
        self.test_evidence = []
        self.store.configure(
            owner,
            {
                "id": TEST_GROUP,
                "name": "测试账本（模拟邀请及时间）",
                "bot_id": self.collector_id,
                "account": TEST_ACCOUNT,
                "enabled": True,
                "rate": 500,
                "agent_rate": 1000,
                "qualification_seconds": 10800,
                "version": 0,
                "contact": "仅测试，不要实际付款",
            },
            self.test_members,
            set(),
            now=joined - 60,
        )
        for uid in ("99000001", "99000002", "99000003"):
            self.test_members[uid] = {
                "name": "测试成员" + uid[-1],
                "peer": "test-" + uid,
                "eligible": True,
            }
            self.test_evidence.append(
                {
                    "member": uid,
                    "inviter": TEST_MANAGER,
                    "source": "business_apply_log",
                    "event_id": "simulated-" + uid,
                    "event_at": joined,
                }
            )
        for stamp in range(int(joined), int(now), 60):
            self.store.snapshot(
                TEST_GROUP,
                self.collector_id,
                TEST_ACCOUNT,
                self.test_members,
                self.test_evidence,
                now=stamp,
            )
        self.store.snapshot(
            TEST_GROUP,
            self.collector_id,
            TEST_ACCOUNT,
            self.test_members,
            self.test_evidence,
            now=now,
        )
        self.store.finance_grant(
            "dashboard/test-window",
            {
                "account": TEST_ACCOUNT,
                "group_id": TEST_GROUP,
                "uid": TEST_MANAGER,
                "peer": manager_peer,
                "name": name,
                "role": "finance_admin",
                "active": True,
                "version": 0,
                "request_id": uuid.uuid4().hex,
            },
        )
