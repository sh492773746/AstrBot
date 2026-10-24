"""Private-admin lotteries and verified, once-only invitation reward accounting."""

import asyncio
import hashlib
import json
import re
import secrets
import time
import unicodedata
import uuid
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from astrbot.core.platform.sources.wangshangliao.database import (
    AsyncDatabase,
    account_guard,
)
from astrbot.core.platform.sources.wangshangliao.event import is_managed_account
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

from .activity_store import database
from .observability import record
from .policy import require_group_admin

PUBLIC_COMMANDS = {
    "参加抽奖",
    "抽奖状态",
    "我的邀请",
    "邀请奖励",
    "我的邀请明细",
    "邀请统计说明",
}
ADMIN_COMMANDS = {
    "抽奖奖励",
    "中奖人数",
    "抽奖倒计时",
    "参与上限",
    "抽奖邀请门槛",
    "领奖联系人",
    "开启抽奖",
    "立即开奖",
    "取消抽奖",
    "中奖名单",
    "抽奖记录",
    "抽奖设置",
    "设置邀请奖励",
    "开启邀请统计",
    "暂停邀请统计",
    "开启邀请奖励",
    "暂停邀请奖励",
    "邀请奖励状态",
    "邀请记录",
    "邀请统计",
}


def clean(value, limit=128, *, multiline=False):
    """Keep user-controlled display values bounded and free of hidden controls.

    Args:
        value: Untrusted nickname, prize or contact.
        limit: Maximum display length.
        multiline: Preserve explicit line breaks for lottery remarks only.

    Returns:
        Plain display text, single-line unless explicitly requested.
    """
    return "".join(
        char
        for char in str(value)
        if char == "\n"
        and multiline
        or ord(char) >= 32
        and unicodedata.category(char) not in {"Cf", "Zl", "Zp"}
    ).strip()[:limit]


def member_name(member, fallback):
    """Return one bounded member-name snapshot without guessing an identity."""
    return clean(
        member.get("groupMemberNick")
        or member.get("userNick")
        or member.get("nickname")
        or member.get("name")
        or fallback
    )


class GroupActivities:
    """Keep lotteries and invitation accounting separate from moderation."""

    def __init__(self, context=None):
        self.context = context
        self.locks = {}
        self.cashback = None

    @staticmethod
    def invitation_tracking_notice(rule):
        """Describe the persisted invitation accounting boundary."""
        started_at = (
            float(rule["tracking_started_at"] or 0)
            if rule and "tracking_started_at" in rule.keys()
            else 0
        )
        if not started_at:
            return "统计起点：尚未记录"
        started = datetime.fromtimestamp(started_at, ZoneInfo("Asia/Shanghai"))
        return f"统计起点：{started.strftime('%m-%d %H:%M:%S')}（此前已入群成员不计入）"

    def lock(self, adapter, group):
        """Serialize one account's group activity transitions.

        Args:
            adapter: Native adapter.
            group: Business group ID.

        Returns:
            Account- and group-scoped asynchronous lock.
        """
        locks = getattr(adapter, "activity_locks", None)
        if locks is None:
            locks = {}
            adapter.activity_locks = locks
        key = (adapter.config["id"], adapter.account, group)
        return locks.setdefault(key, asyncio.Lock())

    def authorize(
        self,
        adapter,
        group,
        owner="",
        session="",
        *,
        posting=False,
        test_window=None,
        test_lottery="",
    ):
        """Revalidate group, account lifecycle and effective creator authority.

        Args:
            adapter: Current native adapter.
            group: Enabled business group.
            owner: Creator UID when checking scheduled work.
            session: Creator's original private profile-routing session.
            posting: Require the saved proactive group grant and reply switch.
            test_window: Optional original controlled lottery window.
            test_lottery: Exact volatile fixture identifier, when already reserved.

        Raises:
            ProtocolError: If current authority or lifecycle is invalid.
        """
        if (
            adapter.config.get("_business_active") is False
            or not adapter.config.get("enable", True)
            or not adapter.account
            or adapter.stopping.is_set()
            or adapter.connection_state != "online"
            or group not in adapter.config.get("enabled_groups", [])
            or group not in adapter.groups
            or adapter.config.get("_open_paused", False)
            or (
                adapter.config.get("_open_integrated")
                and group not in adapter.config.get("_open_groups", {})
            )
        ):
            raise ProtocolError("activity_scope")
        require_group_admin(adapter.config, group)
        if test_window is not None:
            from astrbot.core.platform.sources.wangshangliao.group_scope import (
                GroupAdapter,
            )
            from astrbot.core.platform.sources.wangshangliao.test_window import (
                TestWindow,
            )

            receiver = adapter.adapter if isinstance(adapter, GroupAdapter) else adapter
            if (
                not isinstance(test_window, TestWindow)
                or receiver.account != "43506527"
                or group != "1459282"
                or not receiver.config.get("_open_integrated")
                or test_window.receiver is not receiver
                or receiver.test_window is not test_window
                or test_window.sender.account != "23691273"
                or test_window.groups != [group]
                or "group_commands" not in test_window.scopes
                or not test_window.active()
                or not all(
                    is_managed_account(uid)
                    for uid in ("43506527", "23691273", "31846829")
                )
                or receiver.members.get(group, {}).get("23691273")
                != str(test_window.sender.nim_account)
                or test_lottery
                and getattr(receiver, "_lottery_test_fixture", None)
                != (test_lottery, test_window)
            ):
                raise ProtocolError("lottery_test_window_revoked")
        if (
            owner
            and adapter.config.get("_open_integrated")
            and (owner not in adapter.config.get("_open_managers", {}).get(group, []))
        ):
            raise ProtocolError("activity_owner_revoked")
        if (
            owner
            and not adapter.config.get("_open_integrated")
            and (
                self.context is None
                or owner
                not in {
                    str(uid)
                    for uid in self.context.get_config(session).get("admins_id", [])
                }
            )
        ):
            raise ProtocolError("activity_owner_revoked")
        if posting and (
            adapter.config.get("reply_groups", {}).get(group, True) is False
            or not adapter.config.get("proactive_send", {}).get("enabled", False)
            or group not in adapter.config.get("proactive_send", {}).get("targets", [])
        ):
            raise ProtocolError("activity_proactive_not_authorized")

    async def roster(self, adapter, group):
        """Return a complete, uniquely mapped current membership snapshot.

        Args:
            adapter: Authenticated native adapter.
            group: Enabled business group ID.

        Returns:
            Members indexed by business UID.

        Raises:
            ProtocolError: If membership or lifecycle evidence is incomplete.
        """
        self.authorize(adapter, group)
        account = adapter.account
        page = await adapter.get_moderation_members(group)
        self.authorize(adapter, group)
        if adapter.account != account or not page.get("complete"):
            raise ProtocolError("activity_roster_incomplete")
        members = {}
        peers = set()
        for member in page.get("groupMemberInfo", []):
            uid, peer = str(member.get("userId", "")), str(member.get("nimId", ""))
            if (
                not re.fullmatch(r"[1-9][0-9]{0,19}", uid)
                or not peer.isascii()
                or not peer.isdigit()
                or not 0 < int(peer) < 1 << 32
                or uid in members
                or peer in peers
            ):
                raise ProtocolError("activity_member_identity")
            peers.add(peer)
            members[uid] = {**member, "userId": uid, "nimId": peer}
        if "_open_groups" in adapter.config and members.get(str(account), {}).get(
            "groupRole"
        ) not in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}:
            raise ProtocolError("group_native_admin_required")
        return members

    async def command(
        self, event, group, action, argument, *, precondition=None, test_window=None
    ):
        """Handle exact participation and group-scoped administrator controls.

        Args:
            event: Authenticated native message event.
            group: Current or privately selected authorized group.
            action: Fixed activity command.
            argument: Validated command argument text.
            precondition: Optional bound draft check under the activity lock.
            test_window: Explicit controlled fixture for creation or owner preview.

        Returns:
            Plain command result; no cash payout is performed.
        """
        adapter = event.platform
        if action in {"参加抽奖", "立即开奖"} and test_window is None:
            fixture = getattr(adapter, "_lottery_test_fixture", None)
            if fixture and group == "1459282":
                test_window = fixture[1]
        authorized = (
            str(event.get_sender_id())
            in adapter.config.get("_open_managers", {}).get(group, [])
            if adapter.config.get("_open_integrated")
            else event.is_admin()
        )
        if event.is_private_chat() and not authorized:
            return "无权限"
        group_management = (
            adapter.config.get("_open_integrated")
            and "_open_groups" in adapter.config
            and not event.is_private_chat()
            and str(event.get_group_id()) == str(group)
        )
        if action in ADMIN_COMMANDS and (
            not authorized or not (event.is_private_chat() or group_management)
        ):
            return "无权限"
        if action == "参加抽奖" and event.is_private_chat():
            return "请在活动群发送“参加抽奖”。"
        db_guard = account_guard(
            adapter,
            lambda: self.authorize(
                adapter,
                group,
                event.get_sender_id()
                if self.context and action in ADMIN_COMMANDS
                else "",
                event.unified_msg_origin,
                test_window=test_window,
            ),
        )
        async with self.lock(adapter, group):
            db_guard()
            if precondition is not None:
                await precondition()
            db_guard()
            self.authorize(adapter, group)
            if action in {
                "参加抽奖",
                "抽奖状态",
                "我的邀请",
                "邀请奖励",
                "我的邀请明细",
                "邀请统计说明",
                "中奖名单",
                "抽奖记录",
                "抽奖设置",
                "邀请奖励状态",
                "邀请记录",
                "邀请统计",
            }:
                return await self.query(
                    event, group, action, argument, test_window=test_window
                )
            if self.context is not None:
                self.authorize(
                    adapter, group, event.get_sender_id(), event.unified_msg_origin
                )
            if action in {
                "抽奖奖励",
                "领奖联系人",
                "中奖人数",
                "抽奖倒计时",
                "参与上限",
                "抽奖邀请门槛",
            }:
                text_fields = {"抽奖奖励": "prize", "领奖联系人": "contact"}
                numeric_fields = {
                    "中奖人数": ("winners", 1, 100),
                    "抽奖倒计时": ("duration", 1, 10080),
                    "参与上限": ("capacity", 0, 10000),
                    "抽奖邀请门槛": ("invite_gate", 0, 10000),
                }
                if action in text_fields:
                    value = clean(argument)
                    if not value or value != argument or len(value.encode()) > 512:
                        return "奖品和联系人须为1至128字的单行文字。"
                    field = text_fields[action]
                else:
                    field, low, high = numeric_fields[action]
                    if (
                        not re.fullmatch(r"[0-9]{1,5}", argument)
                        or not low <= int(argument) <= high
                    ):
                        return f"数值范围：{low}至{high}。"
                    value = int(argument) * (60 if field == "duration" else 1)
                async with AsyncDatabase(
                    lambda: database(adapter), write=True, guard=db_guard
                ) as db:
                    if await db.fetchone(
                        "SELECT 1 FROM lotteries WHERE account=? AND group_id=? "
                        "AND status IN ('announcing','open','blocked','drawing')",
                        (adapter.account, group),
                    ):
                        return "活动已开启，请先结束或取消；不会修改正在进行的抽奖。"
                    (
                        await db.execute(
                            "INSERT OR IGNORE INTO lottery_settings(account,group_id) VALUES(?,?)",
                            (adapter.account, group),
                        )
                    )
                    (
                        await db.execute(
                            f"UPDATE lottery_settings SET {field}=? WHERE account=? AND group_id=?",
                            (value, adapter.account, group),
                        )
                    )
                    if field == "invite_gate":
                        await db.execute(
                            "UPDATE lottery_settings SET by_invites=? WHERE account=? AND group_id=?",
                            (int(value > 0), adapter.account, group),
                        )
                return f"已设置{action}：{argument}" + (
                    " 分钟" if action == "抽奖倒计时" else ""
                )
            if action == "开启抽奖":
                return await self.start(event, group, test_window=test_window)
            if action in {"立即开奖", "取消抽奖"}:
                async with AsyncDatabase(
                    lambda: database(adapter), write=False, guard=db_guard
                ) as db:
                    row = await db.fetchone(
                        "SELECT * FROM lotteries WHERE account=? AND group_id=? "
                        "AND status IN ('announcing','open','blocked','drawing') "
                        "ORDER BY created DESC LIMIT 1",
                        (adapter.account, group),
                    )
                if not row:
                    return "没有进行中的抽奖。"
                if action == "立即开奖":
                    if row["status"] not in {"open", "blocked"}:
                        return "通知或开奖结果尚不明确，请查“抽奖状态”，不会重复开奖。"
                    if row["packet_mode"] != "prize" and time.time() < row["ends"]:
                        return "红包抽奖须到设定时间统一公布结果；满员也不提前开奖。"
                    self.authorize(
                        adapter,
                        group,
                        event.get_sender_id(),
                        event.unified_msg_origin,
                        posting=True,
                    )
                    async with AsyncDatabase(
                        lambda: database(adapter), write=True, guard=db_guard
                    ) as db:
                        (
                            await db.execute(
                                "UPDATE lotteries SET owner=?,session=?,status='open',ends=? WHERE id=?",
                                (
                                    event.get_sender_id(),
                                    event.unified_msg_origin,
                                    time.time()
                                    if row["packet_mode"] == "prize"
                                    else row["ends"],
                                    row["id"],
                                ),
                            )
                        )
                    return await self.draw(adapter, row["id"], test_window=test_window)
                if row["status"] == "drawing":
                    return "已经生成中奖结果，不能取消或重新抽取。"
                async with AsyncDatabase(
                    lambda: database(adapter), write=True, guard=db_guard
                ) as db:
                    (
                        await db.execute(
                            "UPDATE lotteries SET status='cancelled',"
                            "unclaimed_amount=CASE WHEN packet_amounts!='[]' "
                            "THEN total_amount ELSE unclaimed_amount END WHERE id=?",
                            (row["id"],),
                        )
                    )
                try:
                    self.authorize(adapter, group, posting=True)
                    await adapter.send_reply_text(
                        group,
                        f"lottery-cancel/{adapter.account}/{row['id']}",
                        "⚠️ 本次抽奖已取消。"
                        + (
                            f"\n未领取退回（未支出）：{Decimal(row['total_amount']) / 100:.2f} 元。"
                            "\n仅记录未支出，不代表已发生真实扣款或退款。"
                            if row["packet_mode"] != "prize"
                            and row["packet_amounts"] != "[]"
                            else ""
                        ),
                        proactive=True,
                        auto_recall=False,
                        authorize=db_guard,
                    )
                except Exception:
                    return "已取消；群通知未确认，不重复提交。"
                return "已取消抽奖。"
            return await self.invitation_command(event, group, action, argument)

    async def query(self, event, group, action, argument="", *, test_window=None):
        """Read scoped activity state or atomically register a current member.

        Args:
            event: Native caller event.
            group: Business group ID.
            action: Fixed query or participation command.
            argument: Optional administrator lottery record number.
            test_window: Original controlled participation window, never a grant.

        Returns:
            Bounded text, without disclosing other members' balances publicly.
        """
        adapter, uid = event.platform, str(event.get_sender_id())
        if action == "参加抽奖" and test_window is None:
            fixture = getattr(adapter, "_lottery_test_fixture", None)
            if fixture and group == "1459282":
                test_window = fixture[1]
        test_entrant = (
            test_window is not None and action == "参加抽奖" and uid == "23691273"
        )
        test_lottery = ""

        def authorize():
            self.authorize(
                adapter,
                group,
                uid if self.context and action in ADMIN_COMMANDS else "",
                event.unified_msg_origin,
                test_window=test_window,
                test_lottery=test_lottery,
            )
            if test_entrant:
                from astrbot.core.platform.sources.wangshangliao.event import (
                    WangshangliaoEvent,
                )

                if (
                    not isinstance(event, WangshangliaoEvent)
                    or event.is_private_chat()
                    or event.get_group_id() != group
                    or event.policy_epoch != adapter.config.get("_business_epoch")
                    or event.get_extra("wsl_test_scope") != "group_commands"
                    or test_window.accepted.get(
                        (group, str(event.message_obj.message_id))
                    )
                    != "group_commands"
                    or event.actor_identity.account != adapter.account
                    or event.actor_identity.verified_uid != uid
                    or event.actor_identity.peer != str(test_window.sender.nim_account)
                    or (event.get_extra("wangshangliao_payload") or {}).get("sender")
                    != uid
                ):
                    raise ProtocolError("lottery_test_message_unverified")

        db_guard = account_guard(adapter, authorize)
        if action == "抽奖设置":
            async with AsyncDatabase(
                lambda: database(adapter), write=True, guard=db_guard
            ) as db:
                (
                    await db.execute(
                        "INSERT OR IGNORE INTO lottery_settings(account,group_id) VALUES(?,?)",
                        (adapter.account, group),
                    )
                )
                settings = await db.fetchone(
                    "SELECT * FROM lottery_settings WHERE account=? AND group_id=?",
                    (adapter.account, group),
                )
            money = (
                f"\n总金额：{Decimal(settings['total_amount']) / 100:.2f} 元"
                f"\n红包类型：{'拼手气' if settings['packet_mode'] == 'lucky' else '普通红包'}"
                if settings["packet_mode"] != "prize"
                else ""
            )
            return (
                f"下期抽奖设置\n奖品：{settings['prize'] or '未设置'}\n"
                f"{money}\n"
                f"中奖人数：{settings['winners']}\n倒计时：{settings['duration'] // 60} 分钟\n"
                f"参与上限：{settings['capacity'] or '不限'}\n"
                f"邀请门槛：{settings['invite_gate']} 人\n"
                f"领奖联系人：{settings['contact'] or '未设置'}"
                + (
                    f"\n活动备注：\n{clean(settings['remarks'], 500, multiline=True)}"
                    if settings["remarks"]
                    else ""
                )
            )
        async with AsyncDatabase(
            lambda: database(adapter), write=False, guard=db_guard
        ) as db:
            if action == "中奖名单" and argument:
                if not re.fullmatch(r"[1-9][0-9]{0,9}", argument):
                    return "格式：中奖名单 <抽奖期数>；不填查询最新一期。"
                row = await db.fetchone(
                    "SELECT rowid AS number,* FROM lotteries WHERE account=? AND group_id=? "
                    "AND rowid=?",
                    (adapter.account, group, int(argument)),
                )
            else:
                row = await db.fetchone(
                    "SELECT rowid AS number,* FROM lotteries WHERE account=? AND group_id=? "
                    "ORDER BY rowid DESC LIMIT 1",
                    (adapter.account, group),
                )
            rule = await db.fetchone(
                "SELECT * FROM invite_rules WHERE account=? AND group_id=?",
                (adapter.account, group),
            )
            own = await db.fetchone(
                "SELECT COUNT(*),COALESCE(SUM(c.amount),0),"
                "COALESCE(SUM(CASE WHEN s.left_at=0 THEN 1 ELSE 0 END),0),"
                "COALESCE(SUM(CASE WHEN s.left_at>0 THEN 1 ELSE 0 END),0) "
                "FROM invite_credits c LEFT JOIN invite_seen s "
                "ON s.account=c.account AND s.group_id=c.group_id AND s.member=c.member "
                "WHERE c.account=? AND c.group_id=? AND c.inviter=?",
                (adapter.account, group, uid),
            )
            own_invitees = ()
            if action == "我的邀请明细":
                own_invitees = await db.fetchall(
                    "SELECT COALESCE(NULLIF(i.member_name,''),'') AS member_name,"
                    "s.left_at FROM invite_credits c "
                    "LEFT JOIN invite_seen s ON s.account=c.account "
                    "AND s.group_id=c.group_id AND s.member=c.member "
                    "LEFT JOIN invite_identities i ON i.account=c.account "
                    "AND i.group_id=c.group_id AND i.member=c.member "
                    "WHERE c.account=? AND c.group_id=? AND c.inviter=? "
                    "ORDER BY c.recorded DESC,c.member LIMIT 20",
                    (adapter.account, group, uid),
                )
            if action == "邀请统计":
                summary = await db.fetchone(
                    "SELECT COUNT(*),"
                    "COALESCE(SUM(CASE WHEN s.left_at=0 THEN 1 ELSE 0 END),0),"
                    "COALESCE(SUM(CASE WHEN s.left_at>0 THEN 1 ELSE 0 END),0) "
                    "FROM invite_credits c LEFT JOIN invite_seen s "
                    "ON s.account=c.account AND s.group_id=c.group_id AND s.member=c.member "
                    "WHERE c.account=? AND c.group_id=?",
                    (adapter.account, group),
                )
                invitation_rows = await db.fetchall(
                    "SELECT c.inviter,"
                    "COALESCE(NULLIF(MAX(i.inviter_name),''),c.inviter) AS inviter_name,"
                    "COUNT(*) AS invited,"
                    "COALESCE(SUM(CASE WHEN s.left_at=0 THEN 1 ELSE 0 END),0) AS active,"
                    "COALESCE(SUM(CASE WHEN s.left_at>0 THEN 1 ELSE 0 END),0) AS left "
                    "FROM invite_credits c "
                    "LEFT JOIN invite_seen s ON s.account=c.account "
                    "AND s.group_id=c.group_id AND s.member=c.member "
                    "LEFT JOIN invite_identities i ON i.account=c.account "
                    "AND i.group_id=c.group_id AND i.member=c.member "
                    "WHERE c.account=? AND c.group_id=? "
                    "GROUP BY c.inviter ORDER BY invited DESC,active DESC,c.inviter LIMIT 20",
                    (adapter.account, group),
                )
        if action == "邀请统计说明":
            return (
                "📌 邀请统计说明\n"
                "只统计统计开启后首次入群、且平台能核验邀请关系的普通成员。\n"
                "待审核、统计前已入群成员、机器人账号和退群后再次入群不会重复计入。\n"
                "入群后会后台核验，通常约 35 秒；可发送“我的邀请”或“我拉了谁”查看本人结果。"
            )
        if action in {"我的邀请", "邀请奖励", "我的邀请明细"} and self.cashback:
            cash_reply = await self.cashback.invitation_reply(event, group, action)
            if cash_reply is not None:
                if action != "我的邀请明细" and rule and (rule["enabled"] or own[1]):
                    cash_reply += f"\n累计积分奖励：{Decimal(own[1]) / 100:.2f} 积分"
                    if rule["enabled"]:
                        cash_reply += (
                            f"\n每人积分奖励：{Decimal(rule['rate']) / 100:.2f} 积分"
                        )
                return cash_reply
        if action == "我的邀请明细":
            if not rule or not rule["tracking_enabled"]:
                return "🎟️ 当前群尚未开启邀请统计。"
            if not own_invitees:
                return "🎟️ 暂无已确认邀请。"
            lines = []
            for index, invitee in enumerate(own_invitees, 1):
                name = clean(invitee["member_name"], 48) or "未命名成员"
                status = "已退群" if invitee["left_at"] else "在群"
                lines.append(f"{index}. {name}（{status}）")
            suffix = (
                f"\n共 {own[0]} 人，仅显示最近 20 人。"
                if own[0] > len(own_invitees)
                else ""
            )
            return "🎟️ 我邀请的成员\n" + "\n".join(lines) + suffix
        if action in {"我的邀请", "邀请奖励"}:
            if not rule or not rule["tracking_enabled"]:
                return "📊 当前群尚未开启邀请统计。"
            rate = Decimal(rule["rate"] if rule else 0) / 100
            presence = (
                f"当前在群：{own[2]} 人\n目前已退群：{own[3]} 人\n"
                if rule and rule["presence_checked_at"]
                else "成员状态：等待完整成员名单刷新\n"
            )
            reward = f"{rate:.2f} 积分" if rule["enabled"] else "管理员未设置奖励"
            return (
                f"累计有效邀请：{own[0]} 人\n{presence}"
                f"累计奖励：{Decimal(own[1]) / 100:.2f} 积分\n"
                f"每人奖励：{reward}"
            )
        if action == "邀请奖励状态":
            return (
                f"邀请统计：{'开启' if rule and rule['tracking_enabled'] else '未开启'}\n"
                f"邀请奖励：{'开启' if rule and rule['enabled'] else '未开启'}\n"
                f"统一每人：{Decimal(rule['rate'] if rule else 0) / 100:.2f} 积分\n"
                f"{self.invitation_tracking_notice(rule)}\n"
                f"最近同步：{datetime.fromtimestamp(rule['last_scan'], ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M:%S') if rule and rule['last_scan'] else '尚未检查'}\n"
                f"成员统计：{'已更新' if rule and rule['presence_checked_at'] else '等待完整名单刷新'}\n"
                f"检查状态：{rule['error'] or '正常' if rule else '未配置'}\n"
                "只记账，不自动付款；历史和待审核邀请不计奖。"
            )
        if action == "邀请统计":
            if not rule or not rule["tracking_enabled"]:
                return "📊 邀请统计\n当前群尚未开启邀请统计。"
            if not invitation_rows:
                return (
                    "📊 邀请统计\n统计已开启，暂无已核验邀请。\n"
                    f"{self.invitation_tracking_notice(rule)}"
                )
            lines = []
            for index, item in enumerate(invitation_rows, 1):
                name = clean(item["inviter_name"], 48) or item["inviter"]
                if rule and rule["presence_checked_at"]:
                    detail = (
                        f"邀请 {item['invited']} 人｜在群 {item['active']} 人｜"
                        f"退群 {item['left']} 人"
                    )
                else:
                    detail = f"累计有效邀请 {item['invited']} 人｜成员状态待刷新"
                lines.append(f"{index}. {name}：{detail}")
            freshness = (
                datetime.fromtimestamp(
                    rule["presence_checked_at"], ZoneInfo("Asia/Shanghai")
                ).strftime("%m-%d %H:%M:%S")
                if rule and rule["presence_checked_at"]
                else "等待下一次完整成员名单刷新"
            )
            head = f"📊 邀请统计\n累计有效邀请：{summary[0]} 人"
            if rule and rule["presence_checked_at"]:
                head += f"\n当前在群：{summary[1]} 人\n目前已退群：{summary[2]} 人"
            return (
                f"{head}\n{self.invitation_tracking_notice(rule)}\n"
                f"名单更新：{freshness}\n" + "\n".join(lines)
            )
        if action == "邀请记录":
            async with AsyncDatabase(
                lambda: database(adapter), write=False, guard=db_guard
            ) as db:
                records = await db.fetchall(
                    "SELECT c.inviter,c.member,c.amount,s.left_at,i.inviter_name,i.member_name "
                    "FROM invite_credits c "
                    "LEFT JOIN invite_seen s ON s.account=c.account "
                    "AND s.group_id=c.group_id AND s.member=c.member "
                    "LEFT JOIN invite_identities i ON i.account=c.account "
                    "AND i.group_id=c.group_id AND i.member=c.member "
                    "WHERE c.account=? AND c.group_id=? ORDER BY c.recorded DESC LIMIT 20",
                    (adapter.account, group),
                )
            return "最近邀请奖励记录：\n" + (
                "\n".join(
                    f"{clean(r['inviter_name'], 36) or r['inviter']} 邀请 "
                    f"{clean(r['member_name'], 36) or r['member']}："
                    f"{Decimal(r['amount']) / 100:.2f} 积分"
                    + (
                        "（已退群）"
                        if rule and rule["presence_checked_at"] and r["left_at"]
                        else ""
                    )
                    for r in records
                )
                or "暂无记录"
            )
        if action == "抽奖记录":
            async with AsyncDatabase(
                lambda: database(adapter), write=False, guard=db_guard
            ) as db:
                records = await db.fetchall(
                    "SELECT rowid AS number,prize,status,total FROM lotteries "
                    "WHERE account=? AND group_id=? ORDER BY rowid DESC LIMIT 20",
                    (adapter.account, group),
                )
            return "最近抽奖记录：\n" + (
                "\n".join(
                    f"第{r['number']}期：{r['prize']}，{r['status']}，{r['total']}人"
                    for r in records
                )
                or "暂无记录"
            )
        if not row:
            return "尚未开启抽奖。"
        if action == "中奖名单":
            winners = json.loads(row["results"])
            if row["status"] not in {"finished", "result_unknown"}:
                return "尚未开奖。"
            return self.result_text(row, winners)[0]
        async with AsyncDatabase(
            lambda: database(adapter), write=False, guard=db_guard
        ) as db:
            count = (
                await db.fetchone(
                    "SELECT COUNT(*) FROM lottery_entries WHERE lottery=?", (row["id"],)
                )
            )[0]
        if action == "抽奖状态":
            now = time.time()
            labels = {
                "announcing": "发布通知中",
                "open": "报名中",
                "blocked": "暂停，待管理员检查",
                "drawing": "开奖中",
                "finished": "已开奖",
                "notice_unknown": "开场通知结果未知，未开放报名",
                "result_unknown": "中奖结果已保存，通知结果未知",
                "cancelled": "已取消",
            }
            label = labels.get(row["status"], row["status"])
            if row["status"] == "open" and now >= row["ends"]:
                label = "报名已截止，等待开奖"
            return (
                f"第{row['number']}期抽奖\n抽奖状态：{label}\n奖品：{row['prize']}\n"
                f"中奖名额：{row['winners']} 人\n已报名：{count} 人\n"
                f"剩余：{max(0, int(row['ends'] - now))} 秒\n"
                f"领奖联系人：{row['contact']}"
                + (
                    f"\n活动备注：\n{clean(row['remarks'], 500, multiline=True)}"
                    if row["remarks"]
                    else ""
                )
                + (
                    f"\n未领取退回（未支出）：{Decimal(row['unclaimed_amount']) / 100:.2f} 元"
                    if row["packet_mode"] != "prize"
                    and row["packet_amounts"] != "[]"
                    and row["status"]
                    in {"finished", "result_unknown", "cancelled", "notice_unknown"}
                    else ""
                )
            )
        if row["status"] != "open" or time.time() >= row["ends"]:
            return "当前没有开放报名的抽奖。"
        if uid == adapter.account or is_managed_account(uid) and not test_entrant:
            return "机器人账号不参与抽奖。"
        if test_entrant:
            test_lottery = row["id"]
            db_guard()
        payload = event.get_extra("wangshangliao_payload") or {}
        stamp = payload.get("created_at", 0)
        if (
            type(stamp) not in (int, float)
            or not row["created"] <= stamp / 1000 <= time.time() + 5
            or time.time() - stamp / 1000 > 300
            or stamp / 1000 >= row["ends"]
        ):
            return "报名消息已过期，请在活动期间重新发送“参加抽奖”。"
        members = await self.roster(adapter, group)
        member = members.get(uid)
        if not member or member.get("accountState") != "ACCOUNT_STATE_GOOD":
            return "成员身份未确认，不能参加抽奖。"
        if test_entrant and member.get("groupRole") != "GROUP_ROLE_MEMBER":
            raise ProtocolError("lottery_test_member_role")
        if test_entrant and str(member["nimId"]) != str(test_window.sender.nim_account):
            raise ProtocolError("lottery_test_member_identity")
        async with AsyncDatabase(
            lambda: database(adapter), write=True, guard=db_guard
        ) as db:
            current = await db.fetchone(
                "SELECT status,ends FROM lotteries WHERE id=?", (row["id"],)
            )
            if current["status"] != "open" or time.time() >= current["ends"]:
                return "报名已截止。"
            if await db.fetchone(
                "SELECT 1 FROM lottery_entries WHERE lottery=? AND member=?",
                (row["id"], uid),
            ):
                return "你已参加本次抽奖，请勿重复报名。"
            count = (
                await db.fetchone(
                    "SELECT COUNT(*) FROM lottery_entries WHERE lottery=?", (row["id"],)
                )
            )[0]
            if row["capacity"] and count >= row["capacity"]:
                return "本次报名名额已满。"
            if row["by_invites"] and own[0] < row["invite_gate"]:
                return f"本次需有效邀请{row['invite_gate']}人；你当前为{own[0]}人。"
            (
                await db.execute(
                    "INSERT INTO lottery_entries VALUES(?,?,?,?,?)",
                    (
                        row["id"],
                        uid,
                        member["nimId"],
                        member_name(member, "")
                        or clean(event.get_sender_name())
                        or uid,
                        time.time(),
                    ),
                )
            )
        return "参加抽奖成功。"

    async def start(self, event, group, *, test_window=None):
        """Snapshot settings and announce one newly opened lottery.

        Args:
            event: Private administrator event.
            group: Privately selected group.
            test_window: Explicit original controlled lottery fixture window.

        Returns:
            Creation or once-only announcement status.
        """
        adapter = event.platform
        identifier = ""
        self.authorize(adapter, group, posting=True, test_window=test_window)
        db_guard = account_guard(
            adapter,
            lambda: self.authorize(
                adapter,
                group,
                event.get_sender_id() if self.context else "",
                event.unified_msg_origin,
                posting=True,
                test_window=test_window,
                test_lottery=identifier,
            ),
        )
        if test_window is not None:
            members = await self.roster(adapter, group)
            db_guard()
            if (
                set(members) != {"31846829", "43506527", "23691273"}
                or members["31846829"].get("groupRole") != "GROUP_ROLE_OWNER"
                or members["43506527"].get("groupRole") != "GROUP_ROLE_ADMIN"
                or members["23691273"].get("groupRole") != "GROUP_ROLE_MEMBER"
                or str(event.get_sender_id()) != "31846829"
                or str(members["43506527"]["nimId"]) != str(adapter.nim_account)
                or str(members["23691273"]["nimId"])
                != str(test_window.sender.nim_account)
                or any(
                    member.get("accountState") != "ACCOUNT_STATE_GOOD"
                    for member in members.values()
                )
            ):
                raise ProtocolError("lottery_test_roster")
        command = hashlib.sha256(
            f"{adapter.account}/{event.unified_msg_origin}/{event.message_obj.message_id}".encode()
        ).hexdigest()
        async with AsyncDatabase(
            lambda: database(adapter), write=True, guard=db_guard
        ) as db:
            prior = await db.fetchone(
                "SELECT status FROM lotteries WHERE command=?", (command,)
            )
            if prior:
                return f"本次开启请求已处理：{prior['status']}。"
            settings = await db.fetchone(
                "SELECT * FROM lottery_settings WHERE account=? AND group_id=?",
                (adapter.account, group),
            )
            if not settings or not settings["prize"] or not settings["contact"]:
                return "请先私聊设置“抽奖奖励 奖品名称”和“领奖联系人 联系方式”。"
            if settings["packet_mode"] != "prize" and (
                settings["packet_mode"] not in {"ordinary", "lucky"}
                or not 1 <= settings["capacity"] <= 100
                or not settings["capacity"] <= settings["total_amount"] <= 99999999
            ):
                return "请先在后台设置红包总金额和1至100位参加人数，每人至少0.01元。"
            if test_window is not None and not settings["prize"].startswith(
                "WSL_TEST_"
            ):
                raise ProtocolError("lottery_test_prize")
            if await db.fetchone(
                "SELECT 1 FROM lotteries WHERE account=? AND group_id=? AND status "
                "IN ('announcing','open','blocked','drawing')",
                (adapter.account, group),
            ):
                return "已有进行中的抽奖，请先结束或取消。"
            if settings["capacity"] and settings["winners"] > settings["capacity"]:
                return "中奖人数不能大于参与上限；参与上限0表示不限。"
            identifier = uuid.uuid4().hex
            amounts = []
            if settings["packet_mode"] != "prize":
                total, count = settings["total_amount"], settings["capacity"]
                if settings["packet_mode"] == "ordinary":
                    share, remainder = divmod(total, count)
                    amounts = [share + (index < remainder) for index in range(count)]
                else:
                    cuts = [
                        0,
                        *sorted(
                            secrets.SystemRandom().sample(range(1, total), count - 1)
                        ),
                        total,
                    ]
                    amounts = [right - left for left, right in zip(cuts, cuts[1:])]
            if test_window is not None:
                adapter._lottery_test_fixture = (identifier, test_window)
            elif group == "1459282":
                adapter._lottery_test_fixture = None
            (
                await db.execute(
                    "INSERT INTO lotteries(id,command,account,group_id,owner,session,owner_name,"
                    "prize,winners,duration,capacity,invite_gate,contact,status,ends,created,"
                    "total_amount,packet_mode,by_invites,remarks,packet_amounts)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'announcing',?,?,?,?,?,?,?)",
                    (
                        identifier,
                        command,
                        adapter.account,
                        group,
                        event.get_sender_id(),
                        event.unified_msg_origin,
                        clean(event.get_sender_name()),
                        settings["prize"],
                        settings["winners"],
                        settings["duration"],
                        settings["capacity"],
                        settings["invite_gate"],
                        settings["contact"],
                        time.time() + settings["duration"],
                        time.time(),
                        settings["total_amount"],
                        settings["packet_mode"],
                        settings["by_invites"],
                        settings["remarks"],
                        json.dumps(amounts),
                    ),
                )
            )
        packet = (
            f"总金额：{Decimal(settings['total_amount']) / 100:.2f} 元\n"
            f"红包类型：{'拼手气' if settings['packet_mode'] == 'lucky' else '普通红包'}\n"
            f"红包份数：{settings['capacity']} 份；到期未领取的金额退回（未支出），不重新分摊。\n"
            "到设定时间统一公布结果，满员也不提前开奖。\n"
            if settings["packet_mode"] != "prize"
            else ""
        )
        gate = (
            f"有效邀请门槛：{settings['invite_gate']} 人\n"
            if settings["by_invites"]
            else ""
        )
        text = (
            f"🎉 抽奖开始！\n奖品：{settings['prize']}\n中奖名额：{settings['winners']} 人\n"
            + packet
            + (
                f"倒计时：{settings['duration'] // 60} 分钟\n"
                f"参与上限：{settings['capacity'] or '不限'} 人\n"
            )
            + gate
            + "群内发送“参与玩家福利抽奖”即可报名。\n"
            + (f"领奖联系人：{settings['contact']}")
            + (
                f"\n活动备注：\n{clean(settings['remarks'], 500, multiline=True)}"
                "\n备注要求由管理员核验，机器人不自动审核头像或昵称。"
                if settings["remarks"]
                else ""
            )
        )
        status = "unknown"
        try:
            db_guard()
            status = await adapter.send_reply_text(
                group,
                f"lottery-start/{adapter.account}/{identifier}",
                text,
                proactive=True,
                auto_recall=False,
                authorize=db_guard,
            )
        finally:
            async with AsyncDatabase(lambda: database(adapter), write=True) as db:
                updated = await db.execute(
                    "UPDATE lotteries SET status=?,error=?,ends=?,"
                    "unclaimed_amount=? WHERE id=? AND status='announcing'",
                    (
                        "open" if status == "accepted" else "notice_unknown",
                        "" if status == "accepted" else status,
                        time.time() + settings["duration"],
                        settings["total_amount"]
                        if amounts and status != "accepted"
                        else 0,
                        identifier,
                    ),
                )
        if not updated:
            return "本期抽奖状态已变化，未重新开启；请查询“抽奖状态”。"
        return (
            f"抽奖已开启：{settings['prize']}，{settings['winners']} 个名额，"
            f"{settings['duration'] // 60} 分钟后开奖。群通知已受理；"
            "发送“抽奖状态”可查询进度。"
            if status == "accepted"
            else "群通知未确认，报名未开启；不会重复发送。"
        )

    def result_text(self, row, winners):
        """Render a unified winner list and native mention spans.

        Args:
            row: Frozen lottery record.
            winners: Persisted winner identities and names.

        Returns:
            Display text and transport mention spans.
        """
        row = dict(row)
        if row.get("packet_mode") == "lucky":
            winners = sorted(winners, key=lambda winner: winner["amount"], reverse=True)
        chance = len(winners) / row["total"] * 100 if row["total"] else 0
        text = f"🎊 抽奖开奖啦！\n总共参与{row['total']}人，综合中奖率{chance:.2f}%\n\n"
        mentions = []
        for index, winner in enumerate(winners, 1):
            text += f"{index}. "
            start = len(text.encode("utf-16-le")) // 2
            nick = clean(winner["name"]) or winner["uid"]
            text += f"@{nick} "
            mentions.append(
                {
                    "uid": int(winner["peer"]),
                    "nick": nick,
                    "start": start,
                    "end": len(text.encode("utf-16-le")) // 2,
                }
            )
            text += (
                f"获得：{Decimal(winner['amount']) / 100:.2f} 元（待发放）\n"
                if "amount" in winner
                else f"获得：{row['prize']}\n"
            )
        if not winners:
            text += "暂无中奖用户。\n"
        if (
            row.get("packet_mode") != "prize"
            and row.get("packet_amounts", "[]") != "[]"
        ):
            allocated = sum(winner["amount"] for winner in winners)
            text += (
                f"\n红包共{row['capacity']}份，本期分配{len(winners)}份"
                f"（待发放）：{Decimal(allocated) / 100:.2f} 元\n"
                f"未领取{row['capacity'] - len(winners)}份，"
                f"退回（未支出）：{Decimal(row['unclaimed_amount']) / 100:.2f} 元\n"
                "仅为分配与未支出记录，不代表已真实付款或退款。\n"
            )
        if row.get("remarks"):
            text += f"\n活动备注：\n{clean(row['remarks'], 500, multiline=True)}\n"
        text += f"\n抽奖创建者：{row['owner_name'] or row['owner']}\n联系 {row['contact']} 领取奖品。"
        return text, mentions

    async def draw(self, adapter, identifier, *, test_window=None):
        """Govern the exact lottery group without replaying unknown publications."""
        governance = getattr(self, "governance", None)
        if governance is None or not governance.policy()["enabled"]:
            return await self._draw(adapter, identifier, test_window=test_window)
        async with AsyncDatabase(
            lambda: database(adapter), guard=account_guard(adapter)
        ) as db:
            row = await db.fetchone(
                "SELECT group_id FROM lotteries WHERE id=?", (identifier,)
            )
        return await governance.run(
            "activities",
            adapter,
            lambda: self._draw(adapter, identifier, test_window=test_window),
            group=row[0] if row else "",
        )

    async def _draw(self, adapter, identifier, *, test_window=None):
        """Persist random winners before publishing a single unified result.

        Args:
            adapter: Current owning native adapter.
            identifier: Existing open lottery identifier.
            test_window: Optional owner-preview window; must match the bound fixture.

        Returns:
            Result publication status without paying rewards.
        """
        fixture = getattr(adapter, "_lottery_test_fixture", None)
        if test_window is not None and fixture != (identifier, test_window):
            raise ProtocolError("lottery_test_fixture_mismatch")
        if fixture and fixture[0] == identifier:
            test_window = fixture[1]
        db_guard = account_guard(adapter)
        async with AsyncDatabase(
            lambda: database(adapter), write=False, guard=db_guard
        ) as db:
            row = await db.fetchone("SELECT * FROM lotteries WHERE id=?", (identifier,))
        if not row or row["status"] != "open" or time.time() < row["ends"]:
            return "尚未到开奖时间，或已经处理。"
        group = row["group_id"]
        self.authorize(adapter, group, row["owner"], row["session"], posting=True)
        if row["account"] != adapter.account:
            raise ProtocolError("activity_account_changed")
        db_guard = account_guard(
            adapter,
            lambda: self.authorize(
                adapter,
                group,
                row["owner"],
                row["session"],
                posting=True,
                test_window=test_window,
                test_lottery=identifier,
            ),
        )
        db_guard()
        members = await self.roster(adapter, group)
        self.authorize(adapter, group, row["owner"], row["session"], posting=True)
        if adapter.config.get("_open_integrated") and row["owner"] not in members:
            raise ProtocolError("activity_owner_revoked")
        async with AsyncDatabase(
            lambda: database(adapter), write=True, guard=db_guard
        ) as db:
            current = await db.fetchone(
                "SELECT status,ends FROM lotteries WHERE id=?", (identifier,)
            )
            if not current or current["status"] != "open":
                return "开奖已处理，不重复抽取。"
            if time.time() < current["ends"]:
                return "尚未到开奖时间，或已经处理。"
            entries = await db.fetchall(
                "SELECT * FROM lottery_entries WHERE lottery=?", (identifier,)
            )
            if any(is_managed_account(r["member"]) for r in entries) and (
                test_window is None
                and row["prize"].startswith("WSL_TEST_")
                and adapter.account == "43506527"
                and group == "1459282"
            ):
                raise ProtocolError("lottery_test_fixture_missing")
            valid = [
                {"uid": r["member"], "peer": r["peer"], "name": r["name"]}
                for r in entries
                if r["member"] in members
                and members[r["member"]]["nimId"] == r["peer"]
                and members[r["member"]].get("accountState") == "ACCOUNT_STATE_GOOD"
                and (
                    not is_managed_account(r["member"])
                    or test_window is not None
                    and r["member"] == "23691273"
                    and r["peer"] == str(test_window.sender.nim_account)
                    and members[r["member"]].get("groupRole") == "GROUP_ROLE_MEMBER"
                )
                and r["member"] != adapter.account
            ]
            fixed_packets = row["packet_amounts"] != "[]"
            try:
                fixed_amounts = json.loads(row["packet_amounts"])
            except (TypeError, ValueError):
                raise ProtocolError("lottery_amount_invalid") from None
            if (
                row["packet_mode"] != "prize"
                and fixed_packets
                and (
                    type(fixed_amounts) is not list
                    or not 1 <= row["capacity"] <= 100
                    or len(fixed_amounts) != row["capacity"]
                    or any(
                        type(amount) is not int or amount < 1
                        for amount in fixed_amounts
                    )
                    or sum(fixed_amounts) != row["total_amount"]
                    or row["packet_mode"] not in {"ordinary", "lucky"}
                )
            ):
                raise ProtocolError("lottery_amount_invalid")
            winners = secrets.SystemRandom().sample(
                valid,
                min(row["capacity"], len(valid))
                if row["packet_mode"] != "prize" and fixed_packets
                else len(valid)
                if row["packet_mode"] != "prize"
                else min(row["winners"], len(valid)),
            )
            unclaimed = 0
            if row["packet_mode"] != "prize" and fixed_packets:
                for winner, amount in zip(
                    winners, fixed_amounts[: len(winners)], strict=True
                ):
                    winner["amount"] = amount
                unclaimed = row["total_amount"] - sum(
                    winner["amount"] for winner in winners
                )
            elif winners and row["packet_mode"] != "prize":
                # Preserve allocations promised by already-open pre-migration draws.
                total, count = row["total_amount"], len(winners)
                if total < count or row["packet_mode"] not in {"ordinary", "lucky"}:
                    raise ProtocolError("lottery_amount_invalid")
                if row["packet_mode"] == "ordinary":
                    share, remainder = divmod(total, count)
                    amounts = [share + (index < remainder) for index in range(count)]
                else:
                    cuts = [
                        0,
                        *sorted(
                            secrets.SystemRandom().sample(range(1, total), count - 1)
                        ),
                        total,
                    ]
                    amounts = [right - left for left, right in zip(cuts, cuts[1:])]
                for winner, amount in zip(winners, amounts, strict=True):
                    winner["amount"] = amount
            (
                await db.execute(
                    "UPDATE lotteries SET status='drawing',results=?,total=?,"
                    "unclaimed_amount=? WHERE id=?",
                    (
                        json.dumps(winners, ensure_ascii=True),
                        len(valid),
                        unclaimed,
                        identifier,
                    ),
                )
            )
        frozen = dict(row)
        frozen["total"] = len(valid)
        frozen["unclaimed_amount"] = unclaimed
        text, mentions = self.result_text(frozen, winners)
        status = "unknown"

        async def authorize_post():
            db_guard()
            if adapter.config.get("_open_integrated"):
                current_members = await self.roster(adapter, group)
                db_guard()
                if row["owner"] not in current_members:
                    raise ProtocolError("activity_owner_revoked")

        try:
            db_guard()
            self.authorize(adapter, group, row["owner"], row["session"], posting=True)
            status = await adapter.send_reply_text(
                group,
                f"lottery-result/{adapter.account}/{identifier}",
                text,
                mentions=mentions or None,
                proactive=True,
                auto_recall=False,
                authorize=authorize_post,
            )
        finally:
            async with AsyncDatabase(lambda: database(adapter), write=True) as db:
                updated = await db.execute(
                    "UPDATE lotteries SET status=?,error=? WHERE id=? AND status='drawing'",
                    (
                        "finished" if status == "accepted" else "result_unknown",
                        "" if status == "accepted" else status,
                        identifier,
                    ),
                )
        if not updated:
            return "本期抽奖状态已变化，未重新发布结果；请查询“抽奖状态”。"
        return (
            "已开奖，统一中奖名单已受理。"
            if status == "accepted"
            else "中奖结果已保存，通知未确认；不会重新抽取。"
        )

    def sync_invitation_presence(
        self,
        db,
        adapter,
        group,
        members,
        now,
        *,
        rate,
        status_for=None,
        include_ineligible=False,
    ):
        """Persist a complete roster snapshot without changing reward credits."""
        db.execute(
            "UPDATE invite_seen SET left_at=? WHERE account=? AND group_id=? "
            "AND status='credited' AND left_at=0",
            (now, adapter.account, group),
        )
        for uid, member in members.items():
            db.execute(
                "UPDATE invite_seen SET peer=?,last_present=?,left_at=0 "
                "WHERE account=? AND group_id=? AND member=?",
                (member["nimId"], now, adapter.account, group, uid),
            )
            if status_for is not None and (
                include_ineligible or member.get("accountState") == "ACCOUNT_STATE_GOOD"
            ):
                db.execute(
                    "INSERT OR IGNORE INTO invite_seen"
                    "(account,group_id,member,peer,first_seen,rate,status,last_present,left_at)"
                    " VALUES(?,?,?,?,?,?,?,?,0)",
                    (
                        adapter.account,
                        group,
                        uid,
                        member["nimId"],
                        now,
                        rate,
                        status_for(uid),
                        now,
                    ),
                )

    @staticmethod
    def attribution_rank(source):
        """Order evidence by how directly it identifies the invitation actor."""
        return {
            "nim_provisional": 1,
            "business_apply_log": 4,
            "nim_notification": 3,
        }.get(source, 0)

    def valid_attribution(self, adapter, group, members, seen, item):
        """Accept only current, exact member and inviter identity evidence."""
        uid, inviter = str(item.get("member_id", "")), str(item.get("inviter_id", ""))
        return (
            uid == seen["member"]
            and item.get("status") == "attributed"
            and inviter != uid
            and inviter != adapter.account
            and uid != adapter.account
            and not is_managed_account(uid)
            and not is_managed_account(inviter)
            and uid in members
            and inviter in members
            and members[uid].get("accountState") == "ACCOUNT_STATE_GOOD"
            and members[inviter].get("accountState") == "ACCOUNT_STATE_GOOD"
            and item.get("member_state", "ACCOUNT_STATE_GOOD") == "ACCOUNT_STATE_GOOD"
            and item.get("inviter_state", "ACCOUNT_STATE_GOOD") == "ACCOUNT_STATE_GOOD"
            and item.get("member_nim_id") == seen["peer"]
            and item.get("member_nim_id") == members[uid]["nimId"]
            and item.get("inviter_nim_id") == members[inviter]["nimId"]
        )

    def save_attribution(
        self, db, adapter, group, members, seen, item, source, event_id, now
    ):
        """Persist a verified attribution and replace lower-confidence fallback."""
        existing = db.execute(
            "SELECT source FROM invite_attributions WHERE account=? AND group_id=? "
            "AND member=?",
            (adapter.account, group, seen["member"]),
        ).fetchone()
        if existing and self.attribution_rank(
            existing["source"]
        ) > self.attribution_rank(source):
            return False
        uid, inviter = seen["member"], item["inviter_id"]
        db.execute(
            "INSERT INTO invite_credits"
            "(account,group_id,member,inviter,member_peer,inviter_peer,amount,recorded) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account,group_id,member) DO UPDATE SET "
            "inviter=excluded.inviter,member_peer=excluded.member_peer,"
            "inviter_peer=excluded.inviter_peer,amount=excluded.amount,"
            "recorded=excluded.recorded",
            (
                adapter.account,
                group,
                uid,
                inviter,
                seen["peer"],
                item["inviter_nim_id"],
                seen["rate"],
                now,
            ),
        )
        db.execute(
            "INSERT OR REPLACE INTO invite_identities"
            "(account,group_id,member,member_name,inviter,inviter_name,updated)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                adapter.account,
                group,
                uid,
                member_name(members[uid], uid),
                inviter,
                member_name(members[inviter], inviter),
                now,
            ),
        )
        db.execute(
            "INSERT INTO invite_attributions"
            "(account,group_id,member,source,event_id,observed) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(account,group_id,member) DO UPDATE SET "
            "source=excluded.source,event_id=excluded.event_id,"
            "observed=excluded.observed",
            (adapter.account, group, uid, source, event_id, now),
        )
        db.execute(
            "UPDATE invite_seen SET status='credited' WHERE account=? AND group_id=? "
            "AND member=?",
            (adapter.account, group, uid),
        )
        return True

    async def invitation_command(self, event, group, action, argument):
        """Configure invitation tracking and one uniform reward rate.

        Args:
            event: Private administrator event.
            group: Selected business group.
            action: Fixed invitation control.
            argument: Optional decimal per-invite reward.

        Returns:
            Configuration status; balances are never retroactively repriced.
        """
        adapter = event.platform
        db_guard = account_guard(
            adapter,
            lambda: self.authorize(
                adapter,
                group,
                event.get_sender_id() if self.context else "",
                event.unified_msg_origin,
            ),
        )
        if action == "设置邀请奖励":
            if not re.fullmatch(r"(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,2})?", argument):
                return "每人奖励须为0至999999.99的数值，最多两位小数。"
            rate = int(Decimal(argument) * 100)
            async with AsyncDatabase(
                lambda: database(adapter), write=True, guard=db_guard
            ) as db:
                (
                    await db.execute(
                        "INSERT INTO invite_rules(account,group_id,rate,owner,session) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(account,group_id) DO UPDATE SET rate=excluded.rate,"
                        "owner=excluded.owner,session=excluded.session",
                        (
                            adapter.account,
                            group,
                            rate,
                            event.get_sender_id(),
                            event.unified_msg_origin,
                        ),
                    )
                )
            return f"统一邀请奖励已设置：每人 {Decimal(rate) / 100:.2f} 积分；已记账奖励不变。"
        if action == "暂停邀请统计":
            async with AsyncDatabase(
                lambda: database(adapter), write=True, guard=db_guard
            ) as db:
                (
                    await db.execute(
                        "UPDATE invite_rules SET tracking_enabled=0,enabled=0 "
                        "WHERE account=? AND group_id=?",
                        (adapter.account, group),
                    )
                )
            return "邀请统计已暂停；邀请奖励同时暂停，已有统计和奖励记录保留。"
        if action == "暂停邀请奖励":
            async with AsyncDatabase(
                lambda: database(adapter), write=True, guard=db_guard
            ) as db:
                (
                    await db.execute(
                        "UPDATE invite_rules SET enabled=0 WHERE account=? AND group_id=?",
                        (adapter.account, group),
                    )
                )
            return "邀请奖励已暂停；邀请统计继续记录人数和退群状态。"
        if action not in {"开启邀请统计", "开启邀请奖励"}:
            return "未知活动命令。"
        async with AsyncDatabase(
            lambda: database(adapter), write=False, guard=db_guard
        ) as db:
            rule = await db.fetchone(
                "SELECT * FROM invite_rules WHERE account=? AND group_id=?",
                (adapter.account, group),
            )
        if action == "开启邀请奖励" and (not rule or rule["rate"] <= 0):
            return "请先发送“设置邀请奖励 数值”，奖励须大于0。"
        if action == "开启邀请统计" and rule and rule["tracking_enabled"]:
            return "邀请统计已开启，不重置基线。"
        if action == "开启邀请奖励" and rule and rule["enabled"]:
            return "邀请奖励已开启，不重置基线。"
        members = await self.roster(adapter, group)
        if not event.is_admin():
            return "无权限"
        self.authorize(adapter, group, event.get_sender_id(), event.unified_msg_origin)
        activation = hashlib.sha256(
            f"{adapter.account}/{event.unified_msg_origin}/{event.message_obj.message_id}".encode()
        ).hexdigest()
        now = time.time()
        tracking_started_at = (
            float(rule["tracking_started_at"] or 0)
            if rule
            and rule["tracking_enabled"]
            and "tracking_started_at" in rule.keys()
            and rule["tracking_started_at"]
            else now
        )
        async with AsyncDatabase(
            lambda: database(adapter), write=True, guard=db_guard
        ) as db:
            if not rule or not rule["tracking_enabled"]:
                (
                    await db.execute(
                        "UPDATE invite_seen SET status='baseline' "
                        "WHERE account=? AND group_id=? AND status<>'credited'",
                        (adapter.account, group),
                    )
                )
                (
                    await db.call(
                        self.sync_invitation_presence,
                        adapter,
                        group,
                        members,
                        now,
                        rate=0,
                        status_for=lambda _uid: "baseline",
                        include_ineligible=True,
                    )
                )
            (
                await db.execute(
                    "INSERT INTO invite_rules(account,group_id,rate,enabled,tracking_enabled,"
                    "tracking_started_at,owner,session,activation,last_scan,presence_checked_at,error) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(account,group_id) DO UPDATE SET "
                    "enabled=excluded.enabled,tracking_enabled=excluded.tracking_enabled,"
                    "tracking_started_at=excluded.tracking_started_at,"
                    "owner=excluded.owner,session=excluded.session,"
                    "activation=excluded.activation,last_scan=excluded.last_scan,"
                    "presence_checked_at=excluded.presence_checked_at,error=''",
                    (
                        adapter.account,
                        group,
                        rule["rate"] if rule else 0,
                        1
                        if action == "开启邀请奖励"
                        else (rule["enabled"] if rule else 0),
                        1,
                        tracking_started_at,
                        event.get_sender_id(),
                        event.unified_msg_origin,
                        activation,
                        now,
                        now,
                        "",
                    ),
                )
            )
        if action == "开启邀请统计":
            return (
                f"邀请统计已开启；{self.invitation_tracking_notice({'tracking_started_at': tracking_started_at})}。"
                f"现有{len(members)}位成员作为基线，后续入群成员开始核验归属。"
            )
        rate = Decimal(rule["rate"]) / 100
        if rule and rule["tracking_enabled"]:
            return f"邀请奖励已开启，每人 {rate:.2f} 积分；仅后续新入群成员计奖，既有统计不倒算。"
        return (
            f"邀请奖励已开启，每人 {rate:.2f} 积分；"
            f"{self.invitation_tracking_notice({'tracking_started_at': tracking_started_at})}，"
            f"现有{len(members)}位成员作为基线不计奖。"
        )

    async def scan_invitations(self, adapter, group):
        """Keep invitation scans in the same group-scoped capability entry."""
        governance = getattr(self, "governance", None)
        if governance is None:
            return await self._scan_invitations(adapter, group)
        return await governance.run(
            "activities",
            adapter,
            lambda: self._scan_invitations(adapter, group),
            group=group,
        )

    async def _scan_invitations(self, adapter, group):
        """Synchronize known invitees and credit newly verified joined members.

        Args:
            adapter: Current account adapter.
            group: Enabled reward group.
        """
        db_guard = account_guard(adapter, lambda: self.authorize(adapter, group))
        async with AsyncDatabase(
            lambda: database(adapter), write=False, guard=db_guard
        ) as db:
            rule = await db.fetchone(
                "SELECT * FROM invite_rules WHERE account=? AND group_id=?",
                (adapter.account, group),
            )
        if (
            not rule
            or not rule["tracking_enabled"]
            or time.time() - max(rule["last_scan"], rule["presence_checked_at"]) < 35
        ):
            return
        db_guard = account_guard(
            adapter,
            lambda: self.authorize(adapter, group, rule["owner"], rule["session"]),
        )
        self.authorize(adapter, group, rule["owner"], rule["session"])
        members = await self.roster(adapter, group)
        self.authorize(adapter, group, rule["owner"], rule["session"])
        if adapter.account != rule["account"]:
            raise ProtocolError("activity_account_changed")
        now = time.time()
        async with AsyncDatabase(
            lambda: database(adapter), write=True, guard=db_guard
        ) as db:
            (
                await db.call(
                    self.sync_invitation_presence,
                    adapter,
                    group,
                    members,
                    now,
                    rate=rule["rate"] if rule["enabled"] else 0,
                    status_for=(
                        (
                            lambda uid: (
                                "ignored"
                                if uid == adapter.account or is_managed_account(uid)
                                else "pending"
                            )
                        )
                        if rule["tracking_enabled"]
                        else None
                    ),
                )
            )
            (
                await db.execute(
                    "UPDATE invite_seen SET status='expired' WHERE account=? AND group_id=? "
                    "AND status='pending' AND first_seen<?",
                    (adapter.account, group, now - 86400),
                )
            )
            pending = await db.fetchall(
                "SELECT seen.* FROM invite_seen AS seen "
                "LEFT JOIN invite_attributions AS attribution "
                "ON attribution.account=seen.account AND attribution.group_id=seen.group_id "
                "AND attribution.member=seen.member "
                "WHERE seen.account=? AND seen.group_id=? AND ("
                "seen.status='pending' OR (seen.status='credited' AND ("
                "attribution.member IS NULL OR (attribution.source IN "
                "('nim_provisional','nim_notification') "
                "AND attribution.observed<?)))) "
                "ORDER BY seen.checked_at,seen.first_seen,seen.member LIMIT 200",
                (adapter.account, group, now - 300),
            )
            (
                await db.execute(
                    "UPDATE invite_rules SET last_scan=?,presence_checked_at=?,error='' "
                    "WHERE account=? AND group_id=?",
                    (now, now, adapter.account, group),
                )
            )
            for seen in pending:
                (
                    await db.execute(
                        "UPDATE invite_seen SET checked_at=? WHERE account=? AND group_id=? AND member=?",
                        (now, adapter.account, group, seen["member"]),
                    )
                )
        selected = [r["member"] for r in pending if r["member"] in members]
        if not selected:
            return
        snapshots = {r["member"]: r for r in pending}
        # A native join operator or retained SDK inviter is not recruitment proof.
        result = await adapter.get_group_inviters(group, members=selected)
        self.authorize(adapter, group, rule["owner"], rule["session"])
        if adapter.account != rule["account"]:
            raise ProtocolError("activity_account_changed")
        if (
            result.get("group_id") != group
            or not result.get("complete")
            or result.get("source") != "business_current_member_inviter"
        ):
            raise ProtocolError("activity_invitation_incomplete")
        async with AsyncDatabase(
            lambda: database(adapter), write=True, guard=db_guard
        ) as db:
            current = await db.fetchone(
                "SELECT tracking_enabled,activation FROM invite_rules "
                "WHERE account=? AND group_id=?",
                (adapter.account, group),
            )
            if (
                not current
                or not current["tracking_enabled"]
                or current["activation"] != rule["activation"]
            ):
                return
            for item in result["items"]:
                uid = item["member_id"]
                seen = snapshots.get(uid)
                if not seen or not self.valid_attribution(
                    adapter, group, members, seen, item
                ):
                    continue
                (
                    await db.call(
                        self.save_attribution,
                        adapter,
                        group,
                        members,
                        seen,
                        item,
                        "business_apply_log",
                        str(item.get("apply_id", "")),
                        now,
                    )
                )

    async def tick(self, adapter):
        """Recover publication boundaries and run only explicitly enabled work.

        Args:
            adapter: Current native adapter.
        """
        if (
            adapter.config.get("_business_active") is False
            or not adapter.config.get("enable", True)
            or not adapter.account
            or adapter.stopping.is_set()
            or adapter.connection_state != "online"
        ):
            return
        epoch = adapter.config.get("_business_epoch")
        db_guard = account_guard(adapter)

        def still_active():
            config = adapter.config
            try:
                db_guard()
            except ProtocolError:
                return False
            return (
                config.get("_business_active") is not False
                and config.get("_business_epoch") == epoch
            )

        path = instance_dir(adapter.config["id"]) / "activities.sqlite3"
        if not path.is_file():
            return
        async with AsyncDatabase(
            lambda: database(adapter), write=False, guard=db_guard
        ) as db:
            rows = await db.fetchall(
                "SELECT * FROM lotteries WHERE account=? AND status IN ('announcing','open','drawing')",
                (adapter.account,),
            )
            rules = await db.fetchall(
                "SELECT group_id FROM invite_rules WHERE account=? "
                "AND tracking_enabled=1",
                (adapter.account,),
            )
        for row in rows:
            group = row["group_id"]
            try:
                require_group_admin(adapter.config, group)
            except ProtocolError:
                continue
            async with self.lock(adapter, group):
                if not still_active():
                    return
                try:
                    if row["status"] in {"announcing", "drawing"}:
                        prefix = (
                            "lottery-start"
                            if row["status"] == "announcing"
                            else "lottery-result"
                        )
                        receipt = await adapter.ledger.receipt(
                            f"{prefix}/{adapter.account}/{row['id']}"
                        )
                        if not still_active():
                            return
                        status = (
                            ("open" if row["status"] == "announcing" else "finished")
                            if receipt and receipt["status"] == "accepted"
                            else (
                                "notice_unknown"
                                if row["status"] == "announcing"
                                else "result_unknown"
                            )
                        )
                        async with AsyncDatabase(
                            lambda: database(adapter), write=True, guard=db_guard
                        ) as db:
                            (
                                await db.execute(
                                    "UPDATE lotteries SET status=?,unclaimed_amount=CASE "
                                    "WHEN ?='notice_unknown' AND packet_amounts!='[]' "
                                    "THEN total_amount ELSE unclaimed_amount END "
                                    "WHERE id=? AND status=?",
                                    (status, status, row["id"], row["status"]),
                                )
                            )
                        record(
                            adapter,
                            "lottery_recovery",
                            status,
                            str(row["id"]),
                            group=group,
                            failed=status in {"notice_unknown", "result_unknown"},
                            aggregate=False,
                        )
                    elif row["ends"] <= time.time():
                        await self.draw(adapter, row["id"])
                except Exception as exc:
                    if not still_active():
                        return
                    async with AsyncDatabase(
                        lambda: database(adapter), write=True, guard=db_guard
                    ) as db:
                        (
                            await db.execute(
                                "UPDATE lotteries SET status='blocked',error=? WHERE id=? AND status='open'",
                                (type(exc).__name__, row["id"]),
                            )
                        )
                    record(
                        adapter,
                        "lottery_tick",
                        "blocked",
                        str(row["id"]),
                        group=group,
                        failed=True,
                        error="operation_failed",
                    )
        for rule in rules:
            try:
                require_group_admin(adapter.config, rule["group_id"])
            except ProtocolError:
                continue
            async with self.lock(adapter, rule["group_id"]):
                if not still_active():
                    return
                try:
                    await self.scan_invitations(adapter, rule["group_id"])
                except Exception as exc:
                    if not still_active():
                        return
                    async with AsyncDatabase(
                        lambda: database(adapter), write=True, guard=db_guard
                    ) as db:
                        (
                            await db.execute(
                                "UPDATE invite_rules SET error=? WHERE account=? AND group_id=?",
                                (type(exc).__name__, adapter.account, rule["group_id"]),
                            )
                        )
                        if (
                            isinstance(exc, ProtocolError)
                            and str(exc) == "activity_owner_revoked"
                        ):
                            (
                                await db.execute(
                                    "UPDATE invite_rules SET enabled=0,tracking_enabled=0,"
                                    "presence_checked_at=0 "
                                    "WHERE account=? AND group_id=?",
                                    (adapter.account, rule["group_id"]),
                                )
                            )
                    record(
                        adapter,
                        "invitation_scan",
                        "rejected",
                        group=rule["group_id"],
                        failed=True,
                        error="permission_denied"
                        if isinstance(exc, ProtocolError)
                        and str(exc) == "activity_owner_revoked"
                        else "operation_failed",
                    )

    async def poll(self):
        """Run one plugin-owned activity worker until cancelled."""
        while True:
            for adapter in list(self.context.platform_manager.platform_insts):
                if adapter.meta().name == "wangshangliao":
                    try:
                        await self.tick(adapter)
                    except Exception:
                        record(
                            adapter,
                            "activity_scan",
                            "failed",
                            failed=True,
                            error="operation_failed",
                        )
            await asyncio.sleep(2)
