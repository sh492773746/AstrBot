"""Private-admin lotteries and verified, once-only invitation reward accounting."""

import asyncio
import hashlib
import json
import re
import secrets
import time
import unicodedata
import uuid
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from astrbot.core.platform.sources.wangshangliao.event import is_managed_account
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

from .activity_store import database
from .observability import record

PUBLIC_COMMANDS = {"参加抽奖", "抽奖状态", "我的邀请", "邀请奖励"}
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
    "开启邀请奖励",
    "暂停邀请奖励",
    "邀请奖励状态",
    "邀请记录",
}


def clean(value, limit=128):
    """Keep user-controlled display values single-line and bounded.

    Args:
        value: Untrusted nickname, prize or contact.
        limit: Maximum display length.

    Returns:
        Plain single-line display text.
    """
    return "".join(
        char
        for char in str(value)
        if ord(char) >= 32 and unicodedata.category(char) not in {"Cf", "Zl", "Zp"}
    ).strip()[:limit]


class GroupActivities:
    """Keep lotteries and invitation accounting separate from moderation."""

    def __init__(self, context=None):
        self.context = context
        self.locks = {}

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

    def authorize(self, adapter, group, owner="", session="", *, posting=False):
        """Revalidate group, account lifecycle and effective creator authority.

        Args:
            adapter: Current native adapter.
            group: Enabled business group.
            owner: Creator UID when checking scheduled work.
            session: Creator's original private profile-routing session.
            posting: Require the saved proactive group grant and reply switch.

        Raises:
            ProtocolError: If current authority or lifecycle is invalid.
        """
        if (
            not adapter.config.get("enable", True)
            or not adapter.account
            or adapter.stopping.is_set()
            or adapter.connection_state != "online"
            or group not in adapter.config.get("enabled_groups", [])
            or group not in adapter.groups
        ):
            raise ProtocolError("activity_scope")
        if owner and (
            self.context is None
            or owner
            not in {
                str(uid)
                for uid in self.context.get_config(session).get("admins_id", [])
            }
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
        return members

    async def command(self, event, group, action, argument):
        """Handle exact public participation and private administrator controls.

        Args:
            event: Authenticated native message event.
            group: Current or privately selected group.
            action: Fixed activity command.
            argument: Validated command argument text.

        Returns:
            Plain command result; no cash payout is performed.
        """
        adapter = event.platform
        if event.is_private_chat() and not event.is_admin():
            return "无权限"
        if action in ADMIN_COMMANDS and (
            not event.is_private_chat() or not event.is_admin()
        ):
            return "无权限"
        if action == "参加抽奖" and event.is_private_chat():
            return "请在活动群发送“参加抽奖”。"
        async with self.lock(adapter, group):
            self.authorize(adapter, group)
            if action in {
                "参加抽奖",
                "抽奖状态",
                "我的邀请",
                "邀请奖励",
                "中奖名单",
                "抽奖记录",
                "抽奖设置",
                "邀请奖励状态",
                "邀请记录",
            }:
                return await self.query(event, group, action, argument)
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
                with closing(database(adapter)) as db, db:
                    if db.execute(
                        "SELECT 1 FROM lotteries WHERE account=? AND group_id=? "
                        "AND status IN ('announcing','open','blocked','drawing')",
                        (adapter.account, group),
                    ).fetchone():
                        return "活动已开启，请先结束或取消；不会修改正在进行的抽奖。"
                    db.execute(
                        "INSERT OR IGNORE INTO lottery_settings(account,group_id) VALUES(?,?)",
                        (adapter.account, group),
                    )
                    db.execute(
                        f"UPDATE lottery_settings SET {field}=? WHERE account=? AND group_id=?",
                        (value, adapter.account, group),
                    )
                return f"已设置{action}：{argument}" + (
                    " 分钟" if action == "抽奖倒计时" else ""
                )
            if action == "开启抽奖":
                return await self.start(event, group)
            if action in {"立即开奖", "取消抽奖"}:
                with closing(database(adapter)) as db:
                    row = db.execute(
                        "SELECT * FROM lotteries WHERE account=? AND group_id=? "
                        "AND status IN ('announcing','open','blocked','drawing') "
                        "ORDER BY created DESC LIMIT 1",
                        (adapter.account, group),
                    ).fetchone()
                if not row:
                    return "没有进行中的抽奖。"
                if action == "立即开奖":
                    if row["status"] not in {"open", "blocked"}:
                        return "通知或开奖结果尚不明确，请查“抽奖状态”，不会重复开奖。"
                    self.authorize(
                        adapter,
                        group,
                        event.get_sender_id(),
                        event.unified_msg_origin,
                        posting=True,
                    )
                    with closing(database(adapter)) as db, db:
                        db.execute(
                            "UPDATE lotteries SET owner=?,session=?,status='open',ends=? WHERE id=?",
                            (
                                event.get_sender_id(),
                                event.unified_msg_origin,
                                time.time(),
                                row["id"],
                            ),
                        )
                    return await self.draw(adapter, row["id"])
                if row["status"] == "drawing":
                    return "已经生成中奖结果，不能取消或重新抽取。"
                with closing(database(adapter)) as db, db:
                    db.execute(
                        "UPDATE lotteries SET status='cancelled' WHERE id=?",
                        (row["id"],),
                    )
                try:
                    self.authorize(adapter, group, posting=True)
                    await adapter.send_reply_text(
                        group,
                        f"lottery-cancel/{adapter.account}/{row['id']}",
                        "⚠️ 本次抽奖已取消。",
                        proactive=True,
                        auto_recall=False,
                    )
                except Exception:
                    return "已取消；群通知未确认，不重复提交。"
                return "已取消抽奖。"
            return await self.invitation_command(event, group, action, argument)

    async def query(self, event, group, action, argument=""):
        """Read scoped activity state or atomically register a current member.

        Args:
            event: Native caller event.
            group: Business group ID.
            action: Fixed query or participation command.
            argument: Optional administrator lottery record number.

        Returns:
            Bounded text, without disclosing other members' balances publicly.
        """
        adapter, uid = event.platform, str(event.get_sender_id())
        if action == "抽奖设置":
            with closing(database(adapter)) as db, db:
                db.execute(
                    "INSERT OR IGNORE INTO lottery_settings(account,group_id) VALUES(?,?)",
                    (adapter.account, group),
                )
                settings = db.execute(
                    "SELECT * FROM lottery_settings WHERE account=? AND group_id=?",
                    (adapter.account, group),
                ).fetchone()
            return (
                f"下期抽奖设置\n奖品：{settings['prize'] or '未设置'}\n"
                f"中奖人数：{settings['winners']}\n倒计时：{settings['duration'] // 60} 分钟\n"
                f"参与上限：{settings['capacity'] or '不限'}\n"
                f"邀请门槛：{settings['invite_gate']} 人\n"
                f"领奖联系人：{settings['contact'] or '未设置'}"
            )
        with closing(database(adapter)) as db:
            if action == "中奖名单" and argument:
                if not re.fullmatch(r"[1-9][0-9]{0,9}", argument):
                    return "格式：中奖名单 <抽奖期数>；不填查询最新一期。"
                row = db.execute(
                    "SELECT rowid AS number,* FROM lotteries WHERE account=? AND group_id=? "
                    "AND rowid=?",
                    (adapter.account, group, int(argument)),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT rowid AS number,* FROM lotteries WHERE account=? AND group_id=? "
                    "ORDER BY rowid DESC LIMIT 1",
                    (adapter.account, group),
                ).fetchone()
            rule = db.execute(
                "SELECT * FROM invite_rules WHERE account=? AND group_id=?",
                (adapter.account, group),
            ).fetchone()
            own = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(amount),0) FROM invite_credits "
                "WHERE account=? AND group_id=? AND inviter=?",
                (adapter.account, group, uid),
            ).fetchone()
        if action in {"我的邀请", "邀请奖励"}:
            rate = Decimal(rule["rate"] if rule else 0) / 100
            return (
                f"有效邀请：{own[0]} 人\n累计奖励：{Decimal(own[1]) / 100:.2f} 积分\n"
                f"每人奖励：{rate:.2f} 积分\n"
                f"奖励状态：{'开启' if rule and rule['enabled'] else '未开启'}"
            )
        if action == "邀请奖励状态":
            return (
                f"邀请奖励：{'开启' if rule and rule['enabled'] else '未开启'}\n"
                f"统一每人：{Decimal(rule['rate'] if rule else 0) / 100:.2f} 积分\n"
                f"最近检查：{datetime.fromtimestamp(rule['last_scan'], ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M:%S') if rule and rule['last_scan'] else '尚未检查'}\n"
                f"检查状态：{rule['error'] or '正常' if rule else '未配置'}\n"
                "只记账，不自动付款；历史和待审核邀请不计奖。"
            )
        if action == "邀请记录":
            with closing(database(adapter)) as db:
                records = db.execute(
                    "SELECT inviter,member,amount FROM invite_credits WHERE account=? "
                    "AND group_id=? ORDER BY recorded DESC LIMIT 20",
                    (adapter.account, group),
                ).fetchall()
            return "最近邀请奖励记录：\n" + (
                "\n".join(
                    f"{r['inviter']} 邀请 {r['member']}：{Decimal(r['amount']) / 100:.2f} 积分"
                    for r in records
                )
                or "暂无记录"
            )
        if action == "抽奖记录":
            with closing(database(adapter)) as db:
                records = db.execute(
                    "SELECT rowid AS number,prize,status,total FROM lotteries "
                    "WHERE account=? AND group_id=? ORDER BY rowid DESC LIMIT 20",
                    (adapter.account, group),
                ).fetchall()
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
        with closing(database(adapter)) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM lottery_entries WHERE lottery=?", (row["id"],)
            ).fetchone()[0]
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
            )
        if row["status"] != "open" or time.time() >= row["ends"]:
            return "当前没有开放报名的抽奖。"
        if uid == adapter.account or is_managed_account(uid):
            return "机器人账号不参与抽奖。"
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
        with closing(database(adapter)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT status,ends FROM lotteries WHERE id=?", (row["id"],)
            ).fetchone()
            if current["status"] != "open" or time.time() >= current["ends"]:
                return "报名已截止。"
            if db.execute(
                "SELECT 1 FROM lottery_entries WHERE lottery=? AND member=?",
                (row["id"], uid),
            ).fetchone():
                return "你已参加本次抽奖，请勿重复报名。"
            count = db.execute(
                "SELECT COUNT(*) FROM lottery_entries WHERE lottery=?", (row["id"],)
            ).fetchone()[0]
            if row["capacity"] and count >= row["capacity"]:
                return "本次报名名额已满。"
            if own[0] < row["invite_gate"]:
                return f"本次需有效邀请{row['invite_gate']}人；你当前为{own[0]}人。"
            db.execute(
                "INSERT INTO lottery_entries VALUES(?,?,?,?,?)",
                (
                    row["id"],
                    uid,
                    member["nimId"],
                    clean(event.get_sender_name()) or uid,
                    time.time(),
                ),
            )
        return "参加抽奖成功。"

    async def start(self, event, group):
        """Snapshot settings and announce one newly opened lottery.

        Args:
            event: Private administrator event.
            group: Privately selected group.

        Returns:
            Creation or once-only announcement status.
        """
        adapter = event.platform
        self.authorize(adapter, group, posting=True)
        command = hashlib.sha256(
            f"{adapter.account}/{event.unified_msg_origin}/{event.message_obj.message_id}".encode()
        ).hexdigest()
        with closing(database(adapter)) as db, db:
            prior = db.execute(
                "SELECT status FROM lotteries WHERE command=?", (command,)
            ).fetchone()
            if prior:
                return f"本次开启请求已处理：{prior['status']}。"
            settings = db.execute(
                "SELECT * FROM lottery_settings WHERE account=? AND group_id=?",
                (adapter.account, group),
            ).fetchone()
            if not settings or not settings["prize"] or not settings["contact"]:
                return "请先私聊设置“抽奖奖励 奖品名称”和“领奖联系人 联系方式”。"
            if db.execute(
                "SELECT 1 FROM lotteries WHERE account=? AND group_id=? AND status "
                "IN ('announcing','open','blocked','drawing')",
                (adapter.account, group),
            ).fetchone():
                return "已有进行中的抽奖，请先结束或取消。"
            if settings["capacity"] and settings["winners"] > settings["capacity"]:
                return "中奖人数不能大于参与上限；参与上限0表示不限。"
            identifier = uuid.uuid4().hex
            db.execute(
                "INSERT INTO lotteries(id,command,account,group_id,owner,session,owner_name,"
                "prize,winners,duration,capacity,invite_gate,contact,status,ends,created)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'announcing',?,?)",
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
                ),
            )
        text = (
            f"🎉 抽奖开始！\n奖品：{settings['prize']}\n中奖名额：{settings['winners']} 人\n"
            f"倒计时：{settings['duration'] // 60} 分钟\n"
            f"参与上限：{settings['capacity'] or '不限'} 人\n"
            f"有效邀请门槛：{settings['invite_gate']} 人\n"
            "群内发送“参加抽奖”即可报名。\n"
            f"领奖联系人：{settings['contact']}"
        )
        status = "unknown"
        try:
            status = await adapter.send_reply_text(
                group,
                f"lottery-start/{adapter.account}/{identifier}",
                text,
                proactive=True,
                auto_recall=False,
            )
        finally:
            with closing(database(adapter)) as db, db:
                db.execute(
                    "UPDATE lotteries SET status=?,error=?,ends=? WHERE id=?",
                    (
                        "open" if status == "accepted" else "notice_unknown",
                        "" if status == "accepted" else status,
                        time.time() + settings["duration"],
                        identifier,
                    ),
                )
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
            text += f"获得：{row['prize']}\n"
        if not winners:
            text += "暂无中奖用户。\n"
        text += f"\n抽奖创建者：{row['owner_name'] or row['owner']}\n联系 {row['contact']} 领取奖品。"
        return text, mentions

    async def draw(self, adapter, identifier):
        """Persist random winners before publishing a single unified result.

        Args:
            adapter: Current owning native adapter.
            identifier: Existing open lottery identifier.

        Returns:
            Result publication status without paying rewards.
        """
        with closing(database(adapter)) as db:
            row = db.execute(
                "SELECT * FROM lotteries WHERE id=?", (identifier,)
            ).fetchone()
        if not row or row["status"] != "open" or time.time() < row["ends"]:
            return "尚未到开奖时间，或已经处理。"
        group = row["group_id"]
        self.authorize(adapter, group, row["owner"], row["session"], posting=True)
        if row["account"] != adapter.account:
            raise ProtocolError("activity_account_changed")
        members = await self.roster(adapter, group)
        self.authorize(adapter, group, row["owner"], row["session"], posting=True)
        with closing(database(adapter)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT status FROM lotteries WHERE id=?", (identifier,)
            ).fetchone()
            if current["status"] != "open":
                return "开奖已处理，不重复抽取。"
            entries = db.execute(
                "SELECT * FROM lottery_entries WHERE lottery=?", (identifier,)
            ).fetchall()
            valid = [
                {"uid": r["member"], "peer": r["peer"], "name": r["name"]}
                for r in entries
                if r["member"] in members
                and members[r["member"]]["nimId"] == r["peer"]
                and members[r["member"]].get("accountState") == "ACCOUNT_STATE_GOOD"
                and not is_managed_account(r["member"])
                and r["member"] != adapter.account
            ]
            winners = secrets.SystemRandom().sample(
                valid, min(row["winners"], len(valid))
            )
            db.execute(
                "UPDATE lotteries SET status='drawing',results=?,total=? WHERE id=?",
                (json.dumps(winners, ensure_ascii=True), len(valid), identifier),
            )
        frozen = dict(row)
        frozen["total"] = len(valid)
        text, mentions = self.result_text(frozen, winners)
        status = "unknown"
        try:
            self.authorize(adapter, group, row["owner"], row["session"], posting=True)
            status = await adapter.send_reply_text(
                group,
                f"lottery-result/{adapter.account}/{identifier}",
                text,
                mentions=mentions or None,
                proactive=True,
                auto_recall=False,
            )
        finally:
            with closing(database(adapter)) as db, db:
                db.execute(
                    "UPDATE lotteries SET status=?,error=? WHERE id=?",
                    (
                        "finished" if status == "accepted" else "result_unknown",
                        "" if status == "accepted" else status,
                        identifier,
                    ),
                )
        return (
            "已开奖，统一中奖名单已受理。"
            if status == "accepted"
            else "中奖结果已保存，通知未确认；不会重新抽取。"
        )

    async def invitation_command(self, event, group, action, argument):
        """Configure one uniform rate and establish an explicit membership baseline.

        Args:
            event: Private administrator event.
            group: Selected business group.
            action: Fixed invitation control.
            argument: Optional decimal per-invite reward.

        Returns:
            Configuration status; balances are never retroactively repriced.
        """
        adapter = event.platform
        if action == "设置邀请奖励":
            if not re.fullmatch(r"(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,2})?", argument):
                return "每人奖励须为0至999999.99的数值，最多两位小数。"
            rate = int(Decimal(argument) * 100)
            with closing(database(adapter)) as db, db:
                db.execute(
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
            return f"统一邀请奖励已设置：每人 {Decimal(rate) / 100:.2f} 积分；已记账奖励不变。"
        if action == "暂停邀请奖励":
            with closing(database(adapter)) as db, db:
                db.execute(
                    "UPDATE invite_rules SET enabled=0 WHERE account=? AND group_id=?",
                    (adapter.account, group),
                )
            return "邀请奖励已暂停；已有奖励记录保留。"
        if action != "开启邀请奖励":
            return "未知活动命令。"
        with closing(database(adapter)) as db:
            rule = db.execute(
                "SELECT * FROM invite_rules WHERE account=? AND group_id=?",
                (adapter.account, group),
            ).fetchone()
        if not rule or rule["rate"] <= 0:
            return "请先发送“设置邀请奖励 数值”，奖励须大于0。"
        if rule["enabled"]:
            return "邀请奖励已开启，不重置基线。"
        members = await self.roster(adapter, group)
        if not event.is_admin():
            return "无权限"
        self.authorize(adapter, group, event.get_sender_id(), event.unified_msg_origin)
        activation = hashlib.sha256(
            f"{adapter.account}/{event.unified_msg_origin}/{event.message_obj.message_id}".encode()
        ).hexdigest()
        with closing(database(adapter)) as db, db:
            db.execute(
                "UPDATE invite_seen SET status='baseline' WHERE account=? AND group_id=? AND status<>'credited'",
                (adapter.account, group),
            )
            for uid, member in members.items():
                db.execute(
                    "INSERT OR IGNORE INTO invite_seen"
                    "(account,group_id,member,peer,first_seen,rate,status)"
                    " VALUES(?,?,?,?,?,?,'baseline')",
                    (adapter.account, group, uid, member["nimId"], time.time(), 0),
                )
            db.execute(
                "UPDATE invite_rules SET enabled=1,owner=?,session=?,activation=?,last_scan=?,error='' "
                "WHERE account=? AND group_id=?",
                (
                    event.get_sender_id(),
                    event.unified_msg_origin,
                    activation,
                    time.time(),
                    adapter.account,
                    group,
                ),
            )
        return f"邀请奖励已开启，每人 {Decimal(rule['rate']) / 100:.2f} 积分；现有{len(members)}位成员作为基线不计奖。"

    async def scan_invitations(self, adapter, group):
        """Credit newly observed joined members with verified native attribution.

        Args:
            adapter: Current account adapter.
            group: Enabled reward group.
        """
        with closing(database(adapter)) as db:
            rule = db.execute(
                "SELECT * FROM invite_rules WHERE account=? AND group_id=? AND enabled=1",
                (adapter.account, group),
            ).fetchone()
        if not rule or time.time() - rule["last_scan"] < 35:
            return
        self.authorize(adapter, group, rule["owner"], rule["session"])
        members = await self.roster(adapter, group)
        self.authorize(adapter, group, rule["owner"], rule["session"])
        if adapter.account != rule["account"]:
            raise ProtocolError("activity_account_changed")
        with closing(database(adapter)) as db, db:
            for uid, member in members.items():
                if member.get("accountState") == "ACCOUNT_STATE_GOOD":
                    db.execute(
                        "INSERT OR IGNORE INTO invite_seen"
                        "(account,group_id,member,peer,first_seen,rate,status)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (
                            adapter.account,
                            group,
                            uid,
                            member["nimId"],
                            time.time(),
                            rule["rate"],
                            "ignored"
                            if uid == adapter.account or is_managed_account(uid)
                            else "pending",
                        ),
                    )
            db.execute(
                "UPDATE invite_seen SET status='expired' WHERE account=? AND group_id=? "
                "AND status='pending' AND first_seen<?",
                (adapter.account, group, time.time() - 86400),
            )
            pending = db.execute(
                "SELECT * FROM invite_seen WHERE account=? AND group_id=? AND status='pending' "
                "ORDER BY checked_at,first_seen,member LIMIT 200",
                (adapter.account, group),
            ).fetchall()
            db.execute(
                "UPDATE invite_rules SET last_scan=?,error='' WHERE account=? AND group_id=?",
                (time.time(), adapter.account, group),
            )
            for seen in pending:
                db.execute(
                    "UPDATE invite_seen SET checked_at=? WHERE account=? AND group_id=? AND member=?",
                    (time.time(), adapter.account, group, seen["member"]),
                )
        selected = [r["member"] for r in pending if r["member"] in members]
        if not selected:
            return
        result = await adapter.get_group_inviters(group, members=selected)
        self.authorize(adapter, group, rule["owner"], rule["session"])
        if adapter.account != rule["account"]:
            raise ProtocolError("activity_account_changed")
        if (
            result.get("group_id") != group
            or not result.get("complete")
            or result.get("source") != "nim_team_member_inviter"
        ):
            raise ProtocolError("activity_invitation_incomplete")
        snapshots = {r["member"]: r for r in pending}
        with closing(database(adapter)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT enabled,activation FROM invite_rules WHERE account=? AND group_id=?",
                (adapter.account, group),
            ).fetchone()
            if (
                not current
                or not current["enabled"]
                or current["activation"] != rule["activation"]
            ):
                return
            for item in result["items"]:
                uid, inviter = item["member_id"], item["inviter_id"]
                seen = snapshots.get(uid)
                if (
                    not seen
                    or item.get("status") != "attributed"
                    or inviter == uid
                    or inviter == adapter.account
                    or uid == adapter.account
                    or is_managed_account(uid)
                    or is_managed_account(inviter)
                    or uid not in members
                    or inviter not in members
                    or members[uid].get("accountState") != "ACCOUNT_STATE_GOOD"
                    or members[inviter].get("accountState") != "ACCOUNT_STATE_GOOD"
                    or item.get("member_state") != "ACCOUNT_STATE_GOOD"
                    or item.get("inviter_state") != "ACCOUNT_STATE_GOOD"
                    or item["member_nim_id"] != seen["peer"]
                    or item["member_nim_id"] != members[uid]["nimId"]
                    or item["inviter_nim_id"] != members[inviter]["nimId"]
                ):
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO invite_credits VALUES(?,?,?,?,?,?,?,?)",
                    (
                        adapter.account,
                        group,
                        uid,
                        inviter,
                        seen["peer"],
                        item["inviter_nim_id"],
                        seen["rate"],
                        time.time(),
                    ),
                )
                db.execute(
                    "UPDATE invite_seen SET status='credited' WHERE account=? AND group_id=? AND member=?",
                    (adapter.account, group, uid),
                )

    async def tick(self, adapter):
        """Recover publication boundaries and run only explicitly enabled work.

        Args:
            adapter: Current native adapter.
        """
        if (
            not adapter.config.get("enable", True)
            or not adapter.account
            or adapter.stopping.is_set()
            or adapter.connection_state != "online"
        ):
            return
        path = instance_dir(adapter.config["id"]) / "activities.sqlite3"
        if not path.is_file():
            return
        with closing(database(adapter)) as db:
            rows = db.execute(
                "SELECT * FROM lotteries WHERE account=? AND status IN ('announcing','open','drawing')",
                (adapter.account,),
            ).fetchall()
            rules = db.execute(
                "SELECT group_id FROM invite_rules WHERE account=? AND enabled=1",
                (adapter.account,),
            ).fetchall()
        for row in rows:
            group = row["group_id"]
            async with self.lock(adapter, group):
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
                        status = (
                            ("open" if row["status"] == "announcing" else "finished")
                            if receipt and receipt["status"] == "accepted"
                            else (
                                "notice_unknown"
                                if row["status"] == "announcing"
                                else "result_unknown"
                            )
                        )
                        with closing(database(adapter)) as db, db:
                            db.execute(
                                "UPDATE lotteries SET status=? WHERE id=? AND status=?",
                                (status, row["id"], row["status"]),
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
                    with closing(database(adapter)) as db, db:
                        db.execute(
                            "UPDATE lotteries SET status='blocked',error=? WHERE id=? AND status='open'",
                            (type(exc).__name__, row["id"]),
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
            async with self.lock(adapter, rule["group_id"]):
                try:
                    await self.scan_invitations(adapter, rule["group_id"])
                except Exception as exc:
                    with closing(database(adapter)) as db, db:
                        db.execute(
                            "UPDATE invite_rules SET error=? WHERE account=? AND group_id=?",
                            (type(exc).__name__, adapter.account, rule["group_id"]),
                        )
                        if (
                            isinstance(exc, ProtocolError)
                            and str(exc) == "activity_owner_revoked"
                        ):
                            db.execute(
                                "UPDATE invite_rules SET enabled=0 WHERE account=? AND group_id=?",
                                (adapter.account, rule["group_id"]),
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
