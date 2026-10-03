"""Point-free, consent-based group challenges with durable draw settlement."""

import json
import re

from telegram.error import TelegramError

from .bet_text import WORDS, parse
from .grant_target import resolve
from .rules import PLAY_NAMES, evaluate
from .store import Rejected, encode

NOTICE = (
    "仅供娱乐；彩头须为自愿、非金钱非财物互动。不扣积分，不托管，不担保履行，不仲裁。"
)
COMMAND = re.compile(
    r"^(对赌|dd)(?:\s*(?=@|同意|拒绝|取消|确认|彩头|选择)(.*)|\s+(.*)|$)",
    re.I | re.S,
)
ACTIVE = "('invited','terms','choosing','pending')"


def select_play(text, room):
    """Validate one selection, ignoring a single syntactically valid amount.

    Args:
        text: Explicit challenge selection without a stake.
        room: Administrator's immutable room configuration.

    Returns:
        A canonical supported play.
    """
    text = text.strip().lower()
    for prefix in ("押注", "选择", "压", "押"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    play = WORDS.get(text)
    if re.fullmatch(r"(?:[0-9]|1[0-9]|2[0-7])(?:点)?", text):
        play = f"number_{int(text.removesuffix('点'))}"
    if re.fullmatch(r"[abc][0-9]", text):
        play = f"pos_{text[0]}_number_{text[1]}"
    if play is None:
        try:
            items = parse(text)
            if len(items) == 1:
                play = items[0][0]
        except Rejected:
            pass
    if play not in room["odds"] or room["odds"][play] <= 0:
        raise Rejected(
            "对赌每人只能选一项，例如：押大、大、ds、bz。不要发送 ds100 bz1 等多项；单项金额会忽略，不扣积分。"
        )
    return play


def outcome(room, first, second, balls):
    """Compare successful selections using frozen administrator multipliers.

    Args:
        room: Frozen administrator configuration.
        first: First player's canonical selection.
        second: Second player's canonical selection.
        balls: Canonical draw digits.

    Returns:
        Winner index (0 for draw, 1 or 2) and both hit flags.
    """
    hits = [evaluate(room, p, 100, balls)[0] != "lose" for p in (first, second)]
    scores = [room["odds"][p] if hit else 0 for p, hit in zip((first, second), hits)]
    return (0 if scores[0] == scores[1] else 1 if scores[0] > scores[1] else 2), hits


class Duel:
    """Persist challenge transitions and replies in the existing single-writer DB."""

    def __init__(self, text_game):
        self.text = text_game
        self.store, self.runtime = text_game.store, text_game.runtime
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS duel_groups(
                chat TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 1,
                version INTEGER NOT NULL DEFAULT 1);
            CREATE INDEX IF NOT EXISTS duel_groups_enabled ON duel_groups(enabled);
            CREATE TABLE IF NOT EXISTS duels(
                id INTEGER PRIMARY KEY,chat TEXT NOT NULL,
                first TEXT NOT NULL,second TEXT NOT NULL,
                first_name TEXT NOT NULL,second_name TEXT NOT NULL,
                status TEXT NOT NULL,expires REAL NOT NULL,
                terms1 TEXT NOT NULL DEFAULT '',terms2 TEXT NOT NULL DEFAULT '',
                yes1 INTEGER NOT NULL DEFAULT 0,yes2 INTEGER NOT NULL DEFAULT 0,
                play1 TEXT NOT NULL DEFAULT '',play2 TEXT NOT NULL DEFAULT '',
                issue INTEGER,snapshot TEXT NOT NULL DEFAULT '{}',
                result TEXT NOT NULL DEFAULT '',created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS duels_chat_active ON duels(chat,status);
            CREATE INDEX IF NOT EXISTS duels_draw ON duels(status,issue);
            CREATE INDEX IF NOT EXISTS duels_expiry ON duels(status,expires);
        """)
        if not self.store.get("duel_groups_initialized", False):
            with self.store.tx() as db:
                db.execute(
                    "INSERT OR IGNORE INTO duel_groups(chat,enabled) SELECT chat,enabled FROM mod_groups"
                )
                self.store.put(db, "duel_groups_initialized", True)

        if "last_source" not in {
            row["name"] for row in self.store.db.execute("PRAGMA table_info(duels)")
        }:
            self.store.db.execute(
                "ALTER TABLE duels ADD COLUMN last_source INTEGER NOT NULL DEFAULT 0"
            )
        self.participants = {
            (row["chat"], row[key])
            for row in self.store.db.execute(
                f"SELECT chat,first,second FROM duels WHERE status IN {ACTIVE}"
            )
            for key in ("first", "second")
        }

    def queue(self, db, chat, body, update=None, reply_to=0, mentions=()):
        """Enqueue one durable reply without touching any wallet.

        Args:
            db: Current transaction.
            chat: Exact group scope.
            body: Plain-text challenge response.
            update: Original interaction, or None for a final result.
        """
        source = (
            update.message.message_id
            if update
            else db.execute(
                "SELECT MIN(COALESCE(MIN(source),0),0)-1 FROM gt_requests WHERE chat=?",
                (chat,),
            ).fetchone()[0]
        )
        db.execute(
            "INSERT INTO gt_requests(chat,source,uid,items,result,text,at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                chat,
                source,
                str(update.effective_user.id) if update else "",
                encode({"reply_to": reply_to, "mentions": mentions})
                if reply_to or mentions
                else "[]",
                "duel" if update else "duel_result",
                body + "\n" + NOTICE,
                self.store.clock(),
            ),
        )
        if update:
            self.text.remember_user(db, update, source)

    async def message(self, update, text):
        """Process explicit commands, membership checks and atomic transitions.

        Args:
            update: Already validated new plain supergroup message.
            text: Original message text.

        Returns:
            Whether the challenge command consumed the message.
        """
        match = COMMAND.fullmatch(text.strip())
        chat, uid = str(update.effective_chat.id), str(update.effective_user.id)
        command = (match[2] or match[3] or "").strip() if match else text.strip()
        if command.lower() in {"确定", "qd"}:
            command = "确认"
        if not match or (
            text.strip().lower() == "dd" and (chat, uid) in self.participants
        ):
            if (chat, uid) not in self.participants:
                return False
            active = self.store.db.execute(
                f"SELECT status FROM duels WHERE chat=? AND status IN {ACTIVE} "
                "AND (status='pending' OR expires>?) AND (first=? OR second=?) "
                "ORDER BY id DESC LIMIT 1",
                (chat, self.store.clock(), uid, uid),
            ).fetchone()
            if not active:
                return False
            if text.strip().lower() == "dd":
                command = "选择 dd" if active["status"] == "choosing" else command
            if command not in {
                "同意",
                "拒绝",
                "取消",
                "确认",
            } and not command.startswith(("彩头", "筹码", "选择", "押注")):
                if active["status"] == "terms":
                    command = "彩头 " + command
                elif active["status"] in {"choosing", "pending"}:
                    command = "选择 " + command
                else:
                    return False
        for alias, action in (("筹码", "彩头"), ("押注", "选择")):
            if command.startswith(alias):
                command = action + " " + command[len(alias) :].strip()
                break
        # Normalize command separators only; preserve whitespace in the user's terms.
        for action in ("彩头", "选择"):
            if command.startswith(action):
                command = action + " " + command[len(action) :].strip()
                break
        now = self.store.clock()
        if len(text) > 512 or not 0 <= now - update.message.date.timestamp() <= 60:
            return True
        if not self.store.get("modules", {}).get("game") or not self.store.get(
            "modules", {}
        ).get("duel", True):
            return True
        if not self.store.db.execute(
            "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1", (chat,)
        ).fetchone():
            return True
        if not self.store.db.execute(
            "SELECT 1 FROM duel_groups WHERE chat=? AND enabled=1", (chat,)
        ).fetchone():
            return True
        if self.store.db.execute(
            "SELECT 1 FROM gt_requests WHERE chat=? AND source=?",
            (chat, update.message.message_id),
        ).fetchone():
            return True
        target, target_name, error = None, "", None
        try:
            await self.runtime.community.member(uid, chat, require_moderation=False)
            if command == "确认" or command.startswith("选择 "):
                active = self.store.db.execute(
                    f"SELECT first,second FROM duels WHERE chat=? AND status IN {ACTIVE} "
                    "AND (first=? OR second=?) ORDER BY id DESC LIMIT 1",
                    (chat, uid, uid),
                ).fetchone()
                if active:
                    other = (
                        active["second"] if active["first"] == uid else active["first"]
                    )
                    await self.runtime.community.member(
                        other, chat, require_moderation=False
                    )
            if not command or command.startswith("@"):
                reply = getattr(update.message, "reply_to_message", None)
                person = getattr(reply, "from_user", None)
                if command.startswith("@"):
                    resolved = await resolve(self.runtime, command)
                    target = resolved["target"]
                    target_name = "@" + resolved["target_username"]
                elif (
                    person
                    and not person.is_bot
                    and not getattr(reply, "sender_chat", None)
                ):
                    target = str(person.id)
                    target_name = (
                        "@" + person.username
                        if getattr(person, "username", None)
                        else f"玩家{person.id}"
                    )
                else:
                    raise Rejected("发送：对赌 @用户名，或回复对方消息发送 dd。")
                if target == uid:
                    raise Rejected("不能向自己发起挑战。")
                member = await self.runtime.bot.get_chat_member(int(chat), int(target))
                if getattr(getattr(member, "user", None), "is_bot", False):
                    raise Rejected("不能向机器人发起挑战。")
                await self.runtime.community.member(
                    target, chat, require_moderation=False
                )
        except Rejected as exc:
            error = str(exc)
        except TelegramError:
            error = "暂时无法核验群成员，请稍后再试。"
        with self.store.tx() as db:
            if db.execute(
                "SELECT 1 FROM gt_requests WHERE chat=? AND source=?",
                (chat, update.message.message_id),
            ).fetchone():
                return True
            if (
                not self.store.get("modules", {}, db).get("game")
                or not self.store.get("modules", {}, db).get("duel", True)
                or not db.execute(
                    "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1", (chat,)
                ).fetchone()
                or not db.execute(
                    "SELECT 1 FROM duel_groups WHERE chat=? AND enabled=1", (chat,)
                ).fetchone()
            ):
                return True
            # A savepoint keeps a rejected operation from partially changing a challenge.
            db.execute("SAVEPOINT duel_command")
            try:
                if error:
                    raise Rejected(error)
                body = self.transition(
                    db, chat, uid, command, target, target_name, update
                )
                db.execute(
                    f"UPDATE duels SET last_source=? WHERE chat=? AND status IN {ACTIVE} "
                    "AND (first=? OR second=?)",
                    (update.message.message_id, chat, uid, uid),
                )
            except Rejected as exc:
                db.execute("ROLLBACK TO duel_command")
                body = str(exc)
            finally:
                db.execute("RELEASE duel_command")
            self.queue(db, chat, body, update)
        if target and not error:
            self.participants.update(((chat, uid), (chat, target)))
        return True

    def transition(self, db, chat, uid, command, target, target_name, update):
        """Apply one authorized transition inside a transaction.

        Args:
            db: Active transaction.
            chat: Group scope.
            uid: Verified actor.
            command: Command tail.
            target: Verified invitation target, if applicable.
            target_name: Verified target label.
            update: Original interaction for the actor label.

        Returns:
            User-facing next step.
        """
        now = self.store.clock()
        if target:
            if db.execute(
                f"SELECT 1 FROM duels WHERE chat=? AND status IN {ACTIVE} "
                "AND (status='pending' OR expires>?) "
                "AND (first IN (?,?) OR second IN (?,?))",
                (chat, now, uid, target, uid, target),
            ).fetchone():
                raise Rejected("你或对方已有进行中的挑战；请先完成或发送 对赌 取消。")
            recent = db.execute(
                "SELECT 1 FROM duels WHERE chat=? AND first=? AND created>?",
                (chat, uid, now - 60),
            ).fetchone()
            if recent:
                raise Rejected("发起邀请间隔至少60秒。")
            name = getattr(update.effective_user, "username", None)
            db.execute(
                "INSERT INTO duels(chat,first,second,first_name,second_name,status,expires,created) "
                "VALUES(?,?,?,?,?,'invited',?,?)",
                (
                    chat,
                    uid,
                    target,
                    "@" + name if name else f"玩家{uid}",
                    target_name,
                    now + 120,
                    now,
                ),
            )
            return f"{target_name} 收到双人挑战邀请。120秒内发送「同意」或「拒绝」。"
        row = db.execute(
            f"SELECT * FROM duels WHERE chat=? AND status IN {ACTIVE} "
            "AND (first=? OR second=?) ORDER BY id DESC LIMIT 1",
            (chat, uid, uid),
        ).fetchone()
        if not row:
            raise Rejected("没有进行中的挑战。回复对方消息发送 dd 可邀请。")
        if row["status"] != "pending" and row["expires"] <= now:
            raise Rejected("挑战已超时，请重新邀请。")
        side = 1 if uid == row["first"] else 2
        if command in {"取消", "拒绝"}:
            if row["status"] == "pending":
                raise Rejected("双方选择已锁定，等待开奖，不能取消。")
            db.execute("UPDATE duels SET status='cancelled' WHERE id=?", (row["id"],))
            return "挑战已取消；未扣积分。"
        if command == "同意":
            if side != 2 or row["status"] != "invited":
                raise Rejected("仅受邀人可接受尚未接受的邀请。")
            db.execute(
                "UPDATE duels SET status='terms',expires=? WHERE id=?",
                (now + 300, row["id"]),
            )
            return "邀请已接受\n🎁 双方5分钟内直接发送彩头文字，例如“唱一首歌”，无需前缀。\n✅ 查看双方彩头后发送“确认”“确定”或“qd”。"
        if command.startswith("彩头 "):
            if row["status"] != "terms":
                raise Rejected("请先让对方发送 同意。")
            terms = command[3:].strip()
            if not 1 <= len(terms) <= 100:
                raise Rejected("彩头说明须为1—100字。")
            if re.search(
                r"现金|转账|红包|人民币|元|块|钱|usdt|cny|trx|btc|充值|兑换|财物|赠送.*(?:车|房)|¥|￥|\$",
                terms,
                re.I,
            ):
                raise Rejected("仅支持非金钱、非财物的自愿互动彩头。")
            db.execute(
                f"UPDATE duels SET terms{side}=?,yes1=0,yes2=0,snapshot='{{}}' WHERE id=?",
                (terms, row["id"]),
            )
            other = row["terms2" if side == 1 else "terms1"]
            return f"筹码已登记\n🎁 您的筹码：“{terms}”\n🎁 对方筹码：“{other or '尚未填写'}”\n修改会清除双方确认。内容公开；接受双方彩头请发送「确认」。"
        if command == "确认":
            if row["status"] != "terms" or not row["terms1"] or not row["terms2"]:
                raise Rejected("请双方先填写彩头。")
            room = self.runtime.game.rooms(db)[self.text.group.group_room(chat)]
            if not room["enabled"]:
                raise Rejected("当前群倍率已停用。")
            snapshot = encode(room)
            if row["snapshot"] not in {"{}", snapshot}:
                raise Rejected("管理员倍率已变化，请重新填写彩头以清除旧确认。")
            db.execute(
                f"UPDATE duels SET yes{side}=1,snapshot=? WHERE id=?",
                (snapshot, row["id"]),
            )
            if row["yes2" if side == 1 else "yes1"]:
                issue, closes = self.runtime.game.current(db)
                db.execute(
                    "UPDATE duels SET status='choosing',issue=?,expires=? WHERE id=?",
                    (issue, min(now + 120, closes), row["id"]),
                )
                return f"双方已确认 · 第{issue}期\n🎯 每人选一项：压大、押大、大、ds、bz均可；允许组合玩法。单项金额忽略，多项不受理。\n🔒 封盘前提交，公开且不可修改。"
            return f"已确认双方彩头，等待对方确认。\n{row['first_name']}：{row['terms1']}\n{row['second_name']}：{row['terms2']}"
        if command.startswith("选择 "):
            if row["status"] != "choosing":
                raise Rejected("请先完成双方彩头确认，或等待已锁定挑战开奖。")
            issue, _ = self.runtime.game.current(db)
            if issue != row["issue"]:
                raise Rejected("已跨期，不能转投下一期，请取消后重新邀请。")
            if row[f"play{side}"]:
                raise Rejected("你的选择已锁定，不能修改。")
            play = select_play(command[3:], json.loads(row["snapshot"]))
            db.execute(f"UPDATE duels SET play{side}=? WHERE id=?", (play, row["id"]))
            other = row["play2" if side == 1 else "play1"]
            if other:
                db.execute("UPDATE duels SET status='pending' WHERE id=?", (row["id"],))
            return (
                f"第{issue}期 · 选择已锁定\n🎁 您的筹码：“{row[f'terms{side}']}”\n🎯 您的玩法：{PLAY_NAMES[play]}\n"
                + (
                    f"🎁 对方筹码：“{row['terms2' if side == 1 else 'terms1']}”\n🎯 对方玩法：{PLAY_NAMES[other]}\n⏳ 双方已选择，等待开奖。"
                    if other
                    else "⏳ 等待对方选择；逾期整场取消。"
                )
            )
        raise Rejected("操作：同意／拒绝／筹码 内容／确认／押注 玩法／取消。")

    def tick(self):
        """Expire incomplete challenges and settle canonical draws exactly once."""
        now = self.store.clock()
        rows = self.store.db.execute(
            f"SELECT * FROM duels WHERE status IN {ACTIVE} "
            "AND ((status='pending' AND EXISTS(SELECT 1 FROM draws "
            "WHERE draws.issue=duels.issue AND conflict=0)) "
            "OR (status!='pending' AND expires<=?)) ORDER BY id LIMIT 100",
            (now,),
        ).fetchall()
        for row in rows:
            with self.store.tx() as db:
                if row["status"] != "pending":
                    db.execute(
                        "UPDATE duels SET status='expired' WHERE id=?", (row["id"],)
                    )
                    body = f"挑战 #{row['id']} 已超时取消；未扣积分。"
                else:
                    draw = db.execute(
                        "SELECT balls FROM draws WHERE issue=? AND conflict=0",
                        (row["issue"],),
                    ).fetchone()
                    if not draw:
                        continue
                    balls = json.loads(draw["balls"])
                    room = json.loads(row["snapshot"])
                    winner, hits = outcome(room, row["play1"], row["play2"], balls)
                    body = f"双人挑战 #{row['id']} · 第{row['issue']}期\n开奖：{' + '.join(map(str, balls))} = {sum(balls)}"
                    for side, hit in enumerate(hits, 1):
                        play = row[f"play{side}"]
                        name = row["first_name" if side == 1 else "second_name"]
                        body += f"\n{name} · {PLAY_NAMES[play]} · {'猜中' if hit else '未中'} · {room['odds'][play] / 100:g}倍"
                    body += "\n🏆 结果：" + (
                        "平局"
                        if not winner
                        else row["first_name" if winner == 1 else "second_name"] + " 胜"
                    )
                    if not any(hits):
                        body += f"\n🔄 {row['first_name']} {row['second_name']}：双方未中，本局作废。请重新发起对赌并选择，不自动沿用旧选择。"
                    body += f"\n🎁 {row['first_name']}的筹码：“{row['terms1']}”\n🎁 {row['second_name']}的筹码：“{row['terms2']}”"
                    db.execute(
                        "UPDATE duels SET status='settled',result=? WHERE id=?",
                        (encode({"winner": winner, "balls": balls}), row["id"]),
                    )
                if (
                    db.execute(
                        "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1",
                        (row["chat"],),
                    ).fetchone()
                    and not db.execute(
                        "SELECT 1 FROM game_panels WHERE duel=?", (row["id"],)
                    ).fetchone()
                ):
                    self.queue(
                        db,
                        row["chat"],
                        body,
                        reply_to=row["last_source"],
                        mentions=(
                            (row["first_name"], row["first"]),
                            (row["second_name"], row["second"]),
                        ),
                    )
