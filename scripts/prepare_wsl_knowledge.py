"""Inventory existing group messages without changing live bot configuration."""

import argparse
import collections
import hashlib
import json
import re
import sqlite3
from pathlib import Path


def redact(text: str) -> str:
    """Remove credentials, destinations and personal identifiers from audit text."""
    text = re.sub(
        r"(?im)^.*(?:密码|验证码|口令|会议\s*ID|会议号|房间号|账号|帐号|接单|群号|旺群|至尊群).*$",
        "[Sensitive line omitted]",
        text,
    )
    text = re.sub(
        r"https?://\S+|(?:[a-zA-Z0-9-]+\.)+(?:com|net|org|top|vip|ws|cn|cc|io)(?:/\S*)?",
        "[Link omitted]",
        text,
    )
    text = re.sub(r"@[\S]+", "[Mention omitted]", text)
    text = re.sub(r"(?<!\d)\d{5,}(?!\d)", "[Identifier omitted]", text)
    return text


def main():
    """Produce a local, redacted corpus and reproducible coverage manifest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = json.loads(Path("data/cmd_config.json").read_text(encoding="utf-8-sig"))
    groups = {
        str(g)
        for p in config.get("platform", [])
        if p.get("type") == "wangshangliao" and p.get("enable")
        for g in p.get("enabled_groups", [])
    }
    records = {}
    for path in sorted(
        Path("data/platform_data/wangshangliao").glob("*/messages.sqlite3")
    ):
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            for group, mid, payload, state in db.execute(
                "SELECT team,message,payload,state FROM inbox"
            ):
                if group not in groups:
                    continue
                data = json.loads(payload)
                text = data.get("text", "") if isinstance(data, dict) else ""
                key = (group, mid)
                candidate = {
                    "group": group,
                    "message_id": mid,
                    "text": text,
                    "timestamp": data.get("created_at")
                    if isinstance(data, dict)
                    else None,
                    "states": {state},
                    "source_count": 1,
                }
                if key not in records:
                    records[key] = candidate
                else:
                    existing = records[key]
                    existing["states"].add(state)
                    existing["source_count"] += 1
                    if len(text) > len(existing["text"]):
                        existing["text"] = text
                    if candidate["timestamp"]:
                        existing["timestamp"] = candidate["timestamp"]
    coverage = {g: collections.Counter() for g in sorted(groups)}
    ledger_texts = {(group, row["text"].strip()) for (group, _), row in records.items()}
    seen = set()
    corpus = []
    for (group, mid), row in sorted(records.items()):
        text = row.pop("text").strip()
        stats = coverage[group]
        stats["unique_messages"] += 1
        stats["with_text"] += bool(text)
        stats["known_source_time"] += bool(row["timestamp"])
        if not text:
            kind = "no_text"
        elif group == "1143980" or re.search(
            r"WSL[-_]|内部检索验收|群管知识库测试", text
        ):
            kind = "test_excluded"
        elif (group, text) in seen:
            kind = "repeat_excluded"
        elif re.fullmatch(r"(?:\[emoticon_\d+\]|[?？\s])+", text):
            kind = "noise_excluded"
        elif re.search(
            r"(?:🇨🇦|大单|小单|大双|小双|\bds\b|\bdd\b|\bxs\b|\bxd\b)", text, re.I
        ):
            kind = "betting_signal_excluded"
        elif re.search(
            r"(充值|返利|彩金|注册链接|传媒.*大秀|争霸赛|接单|开业福利|追梦5000|包下分)",
            text,
            re.S,
        ):
            kind = "promotion_unverified_excluded"
        elif re.search(
            r"开播|直播|下播|浏览器|链接|打不开|看不到|开会|会议|明天8点", text
        ):
            kind = "historical_faq_candidate"
        else:
            kind = "conversation_style_only"
        seen.add((group, text))
        stats[kind] += 1
        row.update(
            category=kind,
            states=sorted(row["states"]),
            text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        )
        row["redacted_text"] = (
            redact(text)
            if kind in {"historical_faq_candidate", "conversation_style_only"}
            else "[Excluded from knowledge]"
        )
        corpus.append(row)
    with sqlite3.connect("file:data/data_v4.db?mode=ro", uri=True) as db:
        history = db.execute(
            "SELECT platform_id,user_id,COUNT(*),MIN(created_at),MAX(created_at) FROM platform_message_history WHERE platform_id LIKE 'wangshangliao_%' AND user_id LIKE '%:GroupMessage:%' GROUP BY platform_id,user_id"
        ).fetchall()
        history_missing = []
        for record_id, session, raw in db.execute(
            "SELECT id,user_id,content FROM platform_message_history "
            "WHERE platform_id LIKE 'wangshangliao_%' AND user_id LIKE '%:GroupMessage:%'"
        ):
            group = session.rsplit("/", 1)[-1]
            if group not in groups:
                continue
            message = json.loads(raw)
            text = "".join(
                part.get("text", "")
                for part in message.get("message", [])
                if part.get("type") == "plain"
            ).strip()
            if text and (group, text) not in ledger_texts:
                history_missing.append(record_id)
    manifest = {
        "groups": coverage,
        "ledger_unique_messages": len(records),
        "history_groups": history,
        "history_unmatched_record_ids": history_missing,
        "limitations": [
            "Local saved messages only; not complete upstream history",
            "Zero timestamps are unknown, not current",
            "No private conversations included",
            "Test group 1143980 excluded from business knowledge",
            "Historical FAQ candidates require editorial review before upload",
        ],
    }
    for name, value in (("coverage.json", manifest), ("redacted-corpus.json", corpus)):
        path = args.output / name
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        path.chmod(0o600)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    if history_missing:
        raise SystemExit(
            "Additional history requires review before claiming full local coverage"
        )


if __name__ == "__main__":
    main()
