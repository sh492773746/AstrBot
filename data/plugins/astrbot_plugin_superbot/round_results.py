"""Publish only newly completed, nonempty group rounds via the short-lived outbox."""

import json

from .rules import PLAY_NAMES, STATUS_NAMES


class RoundResults:
    """Persist one roster snapshot per group/issue, never replaying settled history."""

    def __init__(self, group):
        self.group, self.store = group, group.store
        self.store.db.execute(
            "CREATE TABLE IF NOT EXISTS gt_round_results("
            "chat TEXT NOT NULL,issue INTEGER NOT NULL,status TEXT NOT NULL,"
            "snapshot TEXT NOT NULL DEFAULT '[]',PRIMARY KEY(chat,issue))"
        )
        if not self.store.get("gt_results_initialized", False):
            with self.store.tx() as db:
                db.execute(
                    "INSERT OR IGNORE INTO gt_round_results(chat,issue,status) "
                    "SELECT DISTINCT r.chat,r.issue,'historical' FROM gg_receipts r "
                    "WHERE NOT EXISTS(SELECT 1 FROM gg_receipts other JOIN bets b "
                    "ON b.receipt_op=other.op WHERE other.chat=r.chat "
                    "AND other.issue=r.issue AND b.status='pending')"
                )
                self.store.put(db, "gt_results_initialized", True)

    def tick(self):
        """Atomically snapshot fully settled group rounds and enqueue bounded pages."""
        rounds = self.store.db.execute(
            "SELECT DISTINCT r.chat,r.issue,d.balls,g.enabled FROM gg_receipts r "
            "JOIN draws d ON d.issue=r.issue AND d.conflict=0 "
            "JOIN mod_groups g ON g.chat=r.chat "
            "WHERE NOT EXISTS(SELECT 1 FROM gt_round_results s WHERE s.chat=r.chat AND s.issue=r.issue) "
            "AND EXISTS(SELECT 1 FROM bets b WHERE b.receipt_op=r.op) "
            "AND NOT EXISTS(SELECT 1 FROM gg_receipts other JOIN bets b ON b.receipt_op=other.op "
            "WHERE other.chat=r.chat AND other.issue=r.issue AND b.status='pending') "
            "ORDER BY r.issue LIMIT 10"
        ).fetchall()
        for row in rounds:
            with self.store.tx() as db:
                if not db.execute(
                    "INSERT OR IGNORE INTO gt_round_results(chat,issue,status) VALUES(?,?,'queued')",
                    (row["chat"], row["issue"]),
                ).rowcount:
                    continue
                roster = self.group.broadcast.roster(row["chat"], row["issue"])
                if not row["enabled"] or not roster:
                    db.execute(
                        "UPDATE gt_round_results SET status='suppressed' WHERE chat=? AND issue=?",
                        (row["chat"], row["issue"]),
                    )
                    continue
                db.execute(
                    "UPDATE gt_round_results SET snapshot=? WHERE chat=? AND issue=?",
                    (json.dumps(roster, ensure_ascii=False), row["chat"], row["issue"]),
                )
                balls = json.loads(row["balls"])
                header = (
                    f"加拿大28 · 第 {row['issue']} 期结算名单\n"
                    f"开奖：{' + '.join(map(str, balls))} = {sum(balls)}\n"
                    f"参与 {len(roster)} 人 · 投注 {sum(p['stake'] for p in roster)}"
                    f" · 返还 {sum(p['payout'] for p in roster)}（含本金）\n"
                )
                details = {}
                for bet in db.execute(
                    "SELECT b.uid,b.play,b.amount,b.payout,b.status FROM gg_receipts r "
                    "JOIN bets b ON b.receipt_op=r.op WHERE r.chat=? AND r.issue=? "
                    "ORDER BY r.rowid,b.rowid",
                    (row["chat"], row["issue"]),
                ):
                    details.setdefault(bet["uid"], []).append(
                        f"  {PLAY_NAMES.get(bet['play'], bet['play'])} {bet['amount']} · "
                        f"{STATUS_NAMES.get(bet['status'], bet['status'])} · 返还{bet['payout']}"
                    )
                chunks, lines, size, players = [], [], 0, 0
                folded_lines, mention_lines = set(), {}
                for player in roster:
                    recipient = db.execute(
                        "SELECT username FROM user_labels WHERE uid=?",
                        (str(player["uid"]),),
                    ).fetchone()
                    username = recipient["username"] if recipient else ""
                    name = (
                        str(player["name"]).replace("\n", " ").replace("\r", " ")[:30]
                    )
                    if name == "玩家":
                        name += str(player["uid"])[-4:]
                    if username:
                        name = "@" + username
                    summary = (
                        f"{name} · 投注{player['stake']} · 返还{player['payout']}"
                        f" · 净变动{player['payout'] - player['stake']:+d}"
                    )
                    mention_lines[id(summary)] = (name, str(player["uid"]), username)
                    if len(details.get(player["uid"], [])) > 2:
                        folded_lines.update(id(line) for line in details[player["uid"]])
                    if players >= 20:
                        chunks.append(lines)
                        lines, size, players = [], 0, 0
                    players += 1
                    for line in [summary] + details.get(player["uid"], []):
                        length = len(line.encode("utf-16-le")) // 2 + 1
                        if lines and size + length > 2800:
                            chunks.append(lines)
                            lines = [f"{name}（续）"]
                            mention_lines[id(lines[0])] = (
                                name,
                                str(player["uid"]),
                                username,
                            )
                            size = len(lines[0].encode("utf-16-le")) // 2 + 1
                            players = 1
                        lines.append(line)
                        size += length
                if lines:
                    chunks.append(lines)
                pages = len(chunks)
                source = db.execute(
                    "SELECT MIN(COALESCE(MIN(source),0),0)-1 FROM gt_requests WHERE chat=?",
                    (row["chat"],),
                ).fetchone()[0]
                for page, lines in enumerate(chunks):
                    text = header + f"第 {page + 1}/{pages} 页\n" + "\n".join(lines)
                    db.execute(
                        "INSERT INTO gt_requests(chat,source,uid,issue,items,result,text,at) "
                        "VALUES(?,?,'',?,?,'settlement',?,?)",
                        (
                            row["chat"],
                            source - page,
                            row["issue"],
                            json.dumps(
                                {
                                    "fold_lines": [
                                        i + len(header.splitlines()) + 1
                                        for i, line in enumerate(lines)
                                        if id(line) in folded_lines
                                    ],
                                    "mentions": [
                                        (
                                            i + len(header.splitlines()) + 1,
                                            *mention_lines[id(line)],
                                        )
                                        for i, line in enumerate(lines)
                                        if id(line) in mention_lines
                                    ],
                                }
                            ),
                            text,
                            self.store.clock(),
                        ),
                    )
