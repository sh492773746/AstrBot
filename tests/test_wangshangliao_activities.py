"""Private activity controls, durable lotteries and invitation reward evidence."""

import asyncio
import copy
import json
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation import activities, activity_store
from astrbot.builtin_stars.wangshangliao_moderation.activities import (
    ADMIN_COMMANDS,
    GroupActivities,
)
from astrbot.builtin_stars.wangshangliao_moderation.commands import Commands
from astrbot.builtin_stars.wangshangliao_moderation.syntax import recognize_command
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


def member(uid):
    return {
        "userId": str(uid),
        "nimId": str(900 + int(uid)),
        "userNick": f"Member {uid}",
        "groupMemberNick": f"Member {uid}",
        "accountState": "ACCOUNT_STATE_GOOD",
        "groupRole": "GROUP_ROLE_MEMBER",
    }


@pytest_asyncio.fixture
async def setup(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(activities.time, "time", lambda: now[0])
    monkeypatch.setattr(activities, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(activity_store, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(activities, "is_managed_account", lambda uid: str(uid) == "8")
    roster = {str(uid): member(uid) for uid in [1, 2, 3, 4, 9]}
    ledger = Ledger(tmp_path / "messages.sqlite3")
    await ledger.open()
    adapter = SimpleNamespace(
        account="1",
        groups={"5": "905", "6": "906"},
        config={
            "id": "activity-fixture",
            "enable": True,
            "enabled_groups": ["5", "6"],
            "reply_groups": {"5": True},
            "proactive_send": {"enabled": True, "targets": ["5"]},
        },
        stopping=asyncio.Event(),
        connection_state="online",
        ledger=ledger,
        send_reply_text=AsyncMock(return_value="accepted"),
    )

    async def directory(_group):
        return {"complete": True, "groupMemberInfo": list(roster.values())}

    adapter.get_moderation_members = AsyncMock(side_effect=directory)
    mapping = {}

    async def inviters(group, *, members):
        items = []
        for uid in members:
            inviter = mapping.get(uid, "")
            items.append(
                {
                    "member_id": uid,
                    "member_nim_id": roster[uid]["nimId"],
                    "inviter_id": inviter,
                    "inviter_nim_id": roster[inviter]["nimId"]
                    if inviter in roster
                    else "",
                    "status": "attributed" if inviter in roster else "unattributed",
                    "member_state": roster[uid]["accountState"],
                    "inviter_state": roster[inviter]["accountState"]
                    if inviter in roster
                    else "",
                }
            )
        return {
            "group_id": group,
            "complete": True,
            "source": "nim_team_member_inviter",
            "items": items,
        }

    adapter.get_group_inviters = AsyncMock(side_effect=inviters)
    config = {"admins_id": ["9"]}
    context = SimpleNamespace(get_config=lambda _: config)
    service = GroupActivities(context)
    yield service, adapter, roster, mapping, now, config
    await ledger.close()


def event(adapter, uid="9", *, private=True, admin=None, mid="message"):
    return SimpleNamespace(
        platform=adapter,
        is_private_chat=lambda: private,
        is_admin=lambda: (uid == "9") if admin is None else admin,
        get_sender_id=lambda: uid,
        get_sender_name=lambda: f"Member {uid}",
        get_group_id=lambda: "" if private else "5",
        unified_msg_origin=f"fixture:{'FriendMessage' if private else 'GroupMessage'}:1/{uid if private else '5'}",
        message_obj=SimpleNamespace(message_id=mid),
        get_extra=lambda _: {"created_at": activities.time.time() * 1000},
    )


async def configure(setup, *, capacity=15):
    service, adapter, _, _, _, _ = setup
    caller = event(adapter)
    for action, argument in [
        ("抽奖奖励", "18元猪脚饭"),
        ("中奖人数", "3"),
        ("抽奖倒计时", "1"),
        ("参与上限", str(capacity)),
        ("领奖联系人", "秦铭"),
    ]:
        assert "已设置" in await service.command(caller, "5", action, argument)
    return caller


def latest(adapter):
    with closing(activity_store.database(adapter)) as db:
        return dict(
            db.execute(
                "SELECT * FROM lotteries ORDER BY created DESC,id DESC LIMIT 1"
            ).fetchone()
        )


def credits(adapter):
    with closing(activity_store.database(adapter)) as db:
        return [
            dict(r) for r in db.execute("SELECT * FROM invite_credits ORDER BY member")
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["", "revoked", "expired", "changed", "other_session", "unknown", "exception"])
async def test_natural_lottery_preview_confirmation_and_actual_start(
    setup, tmp_path, monkeypatch, failure
):
    from astrbot.builtin_stars.wangshangliao_moderation import dashboard
    from astrbot.builtin_stars.wangshangliao_moderation.ai_config import (
        AdminConfigDrafts,
    )

    service, adapter, _, _, _, config = setup
    monkeypatch.setattr(dashboard, "instance_dir", lambda _: tmp_path)
    adapter.config["account_id"] = adapter.account
    adapter.group_names = {"5": "测试群"}
    caller = await configure(setup)
    caller.message_str = "请创建测试抽奖"
    drafts = AdminConfigDrafts(service.context, None, service)
    proposal = json.dumps({
        "target": "lottery_start", "group": "测试群",
        "changes": {"lottery": {"prize": "测试无实物奖励", "contact": "测试管理员", "winners": 1}},
    })
    preview = json.loads(await drafts.preview(caller, proposal))
    assert preview["status"] == "preview"
    assert "开启抽奖" in preview["changes"]["operation"]
    assert "确认后开启报名" in preview["display_text"]
    assert "1 分钟到时开奖" in preview["display_text"]
    assert "3 -> 1" in preview["display_text"]
    with closing(activity_store.database(adapter)) as db:
        assert db.execute("SELECT COUNT(*) FROM lotteries").fetchone()[0] == 0
        assert db.execute("SELECT prize FROM lottery_settings").fetchone()[0] == "18元猪脚饭"
    token = preview["confirmation"].split()[-1]
    caller.message_str = f"确认设置 {token}"
    if failure == "revoked":
        config["admins_id"] = []
    elif failure == "expired":
        drafts.drafts[drafts._key(caller)]["expires"] = 0
    elif failure == "changed":
        await service.command(caller, "5", "中奖人数", "2")
    elif failure == "other_session":
        caller.unified_msg_origin += "/other"
    elif failure == "unknown":
        adapter.send_reply_text.return_value = "unknown"
    elif failure == "exception":
        adapter.send_reply_text.side_effect = ProtocolError("send_unknown")
    result = json.loads(await drafts.confirm(caller, token))
    if failure in {"revoked", "expired", "changed", "other_session"}:
        assert result["status"] == "rejected"
        with closing(activity_store.database(adapter)) as db:
            assert db.execute("SELECT COUNT(*) FROM lotteries").fetchone()[0] == 0
        assert adapter.send_reply_text.await_count == 0
        return
    assert result["status"] == ("not_confirmed" if failure else "started")
    row = latest(adapter)
    assert row["id"] == result["lottery_id"]
    assert row["status"] == ("notice_unknown" if failure else "open")
    assert row["prize"] == "测试无实物奖励" and row["winners"] == 1
    assert adapter.send_reply_text.await_count == 1
    assert adapter.send_reply_text.await_args.kwargs["auto_recall"] is False
    assert json.loads(await drafts.confirm(caller, token))["status"] == "rejected"
    assert adapter.send_reply_text.await_count == 1
    # Unknown or active announcements must not be recreated by a new proposal.
    assert json.loads(await drafts.preview(caller, proposal))["status"] == "rejected"


@pytest.mark.asyncio
async def test_fifteen_entries_three_winners_and_single_native_notification(setup):
    service, adapter, roster, _, now, _ = setup
    caller = await configure(setup)
    opened = await service.command(caller, "5", "开启抽奖", "")
    assert "已开启：18元猪脚饭，3 个名额，1 分钟后开奖" in opened
    assert "群通知已受理" in opened and "抽奖状态" in opened
    for uid in range(20, 35):
        roster[str(uid)] = member(uid)
        assert (
            await service.command(
                event(adapter, str(uid), private=False), "5", "参加抽奖", ""
            )
            == "参加抽奖成功。"
        )
    now[0] += 61
    await service.tick(adapter)
    row = latest(adapter)
    assert row["status"] == "finished" and row["total"] == 15
    results = json.loads(row["results"])
    assert len(results) == 3 and len({r["uid"] for r in results}) == 3
    call = adapter.send_reply_text.await_args
    assert "总共参与15人，综合中奖率20.00%" in call.args[2]
    assert "联系 秦铭 领取奖品" in call.args[2]
    assert len(call.kwargs["mentions"]) == 3
    assert call.kwargs["auto_recall"] is False and call.kwargs["proactive"] is True
    await service.tick(adapter)
    assert adapter.send_reply_text.await_count == 2
    assert json.loads(latest(adapter)["results"]) == results


@pytest.mark.asyncio
async def test_concurrent_duplicate_signup_and_capacity(setup):
    service, adapter, _, _, _, _ = setup
    caller = await configure(setup, capacity=3)
    await service.command(caller, "5", "开启抽奖", "")
    user = event(adapter, "2", private=False)
    replies = await asyncio.gather(
        *[service.command(user, "5", "参加抽奖", "") for _ in range(8)]
    )
    assert replies.count("参加抽奖成功。") == 1
    assert replies.count("你已参加本次抽奖，请勿重复报名。") == 7
    for uid in ["3", "4"]:
        assert "成功" in await service.command(
            event(adapter, uid, private=False), "5", "参加抽奖", ""
        )
    assert "已满" in await service.command(
        event(adapter, "9", private=False), "5", "参加抽奖", ""
    )


@pytest.mark.asyncio
async def test_settings_frozen_after_open_and_deadline_rejects_signup(setup):
    service, adapter, _, _, now, _ = setup
    caller = await configure(setup)
    await service.command(caller, "5", "开启抽奖", "")
    assert "不会修改" in await service.command(caller, "5", "抽奖奖励", "新奖品")
    now[0] += 60
    assert "没有开放" in await service.command(
        event(adapter, "2", private=False), "5", "参加抽奖", ""
    )
    assert latest(adapter)["prize"] == "18元猪脚饭"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "private,admin", [(False, True), (False, False), (True, False)]
)
async def test_all_activity_admin_controls_require_private_authority(
    setup, private, admin
):
    service, adapter, _, _, _, _ = setup
    caller = event(adapter, "2", private=private, admin=admin)
    for action in ADMIN_COMMANDS:
        assert await service.command(caller, "5", action, "5") == "无权限"
    adapter.send_reply_text.assert_not_awaited()
    adapter.get_moderation_members.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_command_dispatch_public_queries_and_private_selection(setup):
    service, adapter, _, _, _, _ = setup
    commands = Commands(activities=service)
    public = event(adapter, "2", private=False, admin=False)
    assert await commands.run(public, "抽奖状态") == "尚未开启抽奖。"
    assert "0 人" in await commands.run(public, "我的邀请")
    assert await commands.run(public, "设置邀请奖励 5") is None
    private = event(adapter)
    assert "目标群未启用" in await commands.run(private, "抽奖奖励 奖品")
    await commands.run(private, "群列表")
    await commands.run(private, "选择群 1")
    assert "已设置" in await commands.run(private, "抽奖奖励 奖品")
    assert await commands.run(event(adapter, "2"), "开启抽奖") == "无权限"


@pytest.mark.asyncio
async def test_no_proactive_grant_no_announcement_and_no_implicit_grants(setup):
    service, adapter, _, _, _, _ = setup
    caller = await configure(setup)
    before = copy.deepcopy(adapter.config)
    adapter.config["proactive_send"]["targets"] = []
    with pytest.raises(ProtocolError, match="proactive"):
        await service.command(caller, "5", "开启抽奖", "")
    adapter.send_reply_text.assert_not_awaited()
    assert adapter.config["enabled_groups"] == before["enabled_groups"]


@pytest.mark.asyncio
async def test_removed_or_changed_members_do_not_win_and_empty_draw_finishes(setup):
    service, adapter, roster, _, now, _ = setup
    caller = await configure(setup)
    await service.command(caller, "5", "开启抽奖", "")
    for uid in ["2", "3"]:
        await service.command(event(adapter, uid, private=False), "5", "参加抽奖", "")
    roster.pop("2")
    roster["3"]["nimId"] = "1999"
    now[0] += 61
    await service.tick(adapter)
    row = latest(adapter)
    assert row["status"] == "finished"
    assert json.loads(row["results"]) == [] and row["total"] == 0
    assert "暂无中奖用户" in adapter.send_reply_text.await_args.args[2]


@pytest.mark.asyncio
async def test_unknown_publication_retains_winners_without_reroll(setup):
    service, adapter, _, _, now, _ = setup
    caller = await configure(setup)
    await service.command(caller, "5", "开启抽奖", "")
    await service.command(event(adapter, "2", private=False), "5", "参加抽奖", "")
    adapter.send_reply_text.return_value = "unknown"
    now[0] += 61
    await service.tick(adapter)
    row = latest(adapter)
    assert row["status"] == "result_unknown" and len(json.loads(row["results"])) == 1
    await service.tick(adapter)
    assert adapter.send_reply_text.await_count == 2
    assert "Member 2" in await service.command(caller, "5", "中奖名单", "")


@pytest.mark.asyncio
async def test_restart_uncertain_boundary_never_republishes_or_rerolls(setup):
    service, adapter, _, _, now, _ = setup
    caller = await configure(setup)
    await service.command(caller, "5", "开启抽奖", "")
    row = latest(adapter)
    with closing(activity_store.database(adapter)) as db, db:
        db.execute(
            "UPDATE lotteries SET status='drawing',results=?,total=1 WHERE id=?",
            (json.dumps([{"uid": "2", "peer": "902", "name": "Member 2"}]), row["id"]),
        )
    now[0] += 61
    restarted = GroupActivities(service.context)
    await restarted.tick(adapter)
    assert latest(adapter)["status"] == "result_unknown"
    assert adapter.send_reply_text.await_count == 1


@pytest.mark.asyncio
async def test_offline_resume_and_creator_revocation(setup):
    service, adapter, _, _, now, config = setup
    caller = await configure(setup)
    await service.command(caller, "5", "开启抽奖", "")
    now[0] += 61
    adapter.connection_state = "reconnecting"
    await service.tick(adapter)
    assert latest(adapter)["status"] == "open"
    adapter.connection_state = "online"
    config["admins_id"] = []
    await service.tick(adapter)
    assert latest(adapter)["status"] == "blocked"
    assert adapter.send_reply_text.await_count == 1
    config["admins_id"] = ["9"]
    assert "已开奖" in await service.command(caller, "5", "立即开奖", "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,value",
    [
        ("中奖人数", "0"),
        ("中奖人数", "101"),
        ("中奖人数", "３"),
        ("抽奖倒计时", "0"),
        ("抽奖倒计时", "10081"),
        ("参与上限", "-1"),
        ("参与上限", "10001"),
        ("抽奖邀请门槛", "1.5"),
        ("领奖联系人", ""),
        ("设置邀请奖励", "-1"),
        ("设置邀请奖励", "1.001"),
        ("设置邀请奖励", "1000000"),
        ("设置邀请奖励", "NaN"),
    ],
)
async def test_invalid_settings_never_post_or_award(setup, action, value):
    service, adapter, _, _, _, _ = setup
    response = await service.command(event(adapter), "5", action, value)
    assert "已设置" not in response
    assert credits(adapter) == []
    adapter.send_reply_text.assert_not_awaited()


async def enable_rewards(setup, rate="5"):
    service, adapter, _, _, _, _ = setup
    caller = event(adapter)
    assert "已设置" in await service.command(caller, "5", "设置邀请奖励", rate)
    assert "已开启" in await service.command(caller, "5", "开启邀请奖励", "")
    return caller


@pytest.mark.asyncio
async def test_baseline_then_native_invitation_reward_exactly_once(setup):
    service, adapter, roster, mapping, now, _ = setup
    await enable_rewards(setup, "5.25")
    mapping["3"] = "2"
    now[0] += 36
    await service.tick(adapter)
    assert credits(adapter) == []
    roster["6"] = member("6")
    mapping["6"] = "2"
    now[0] += 36
    await service.tick(adapter)
    assert len(credits(adapter)) == 1
    assert (
        credits(adapter)[0]["amount"] == 525 and credits(adapter)[0]["inviter"] == "2"
    )
    now[0] += 36
    await service.tick(adapter)
    roster.pop("6")
    now[0] += 36
    await service.tick(adapter)
    roster["6"] = member("6")
    mapping["6"] = "4"
    now[0] += 36
    await service.tick(adapter)
    assert len(credits(adapter)) == 1 and credits(adapter)[0]["inviter"] == "2"
    result = await service.command(
        event(adapter, "2", private=False), "5", "我的邀请", ""
    )
    assert "1 人" in result and "5.25 积分" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["pending", "unattributed", "self", "bot", "bad_peer", "banned"]
)
async def test_pending_ambiguous_self_bot_or_mismatched_invites_never_award(
    setup, kind
):
    service, adapter, roster, mapping, now, _ = setup
    await enable_rewards(setup)
    uid = "8" if kind == "bot" else "6"
    if kind != "pending":
        roster[uid] = member(uid)
    mapping[uid] = "" if kind == "unattributed" else uid if kind == "self" else "2"
    if kind == "banned":
        roster[uid]["accountState"] = "ACCOUNT_STATE_BAN"
    if kind == "bad_peer":
        original = adapter.get_group_inviters.side_effect

        async def forged(group, *, members):
            result = await original(group, members=members)
            result["items"][0]["inviter_nim_id"] = "9999"
            return result

        adapter.get_group_inviters.side_effect = forged
    now[0] += 36
    await service.tick(adapter)
    assert credits(adapter) == []


@pytest.mark.asyncio
async def test_rate_changes_do_not_reprice_pending_or_paid_credits(setup):
    service, adapter, roster, mapping, now, _ = setup
    caller = await enable_rewards(setup)
    roster["6"] = member("6")
    now[0] += 36
    await service.tick(adapter)
    assert credits(adapter) == []
    await service.command(caller, "5", "设置邀请奖励", "10")
    mapping["6"] = "2"
    roster["7"] = member("7")
    mapping["7"] = "2"
    now[0] += 36
    await service.tick(adapter)
    assert {r["member"]: r["amount"] for r in credits(adapter)} == {"6": 500, "7": 1000}


@pytest.mark.asyncio
async def test_pause_resume_rebaselines_and_preserves_credits(setup):
    service, adapter, roster, mapping, now, _ = setup
    caller = await enable_rewards(setup)
    await service.command(caller, "5", "暂停邀请奖励", "")
    roster["6"] = member("6")
    mapping["6"] = "2"
    now[0] += 36
    await service.tick(adapter)
    await service.command(caller, "5", "开启邀请奖励", "")
    now[0] += 36
    await service.tick(adapter)
    assert credits(adapter) == []
    roster["7"] = member("7")
    mapping["7"] = "2"
    now[0] += 36
    await service.tick(adapter)
    assert [r["member"] for r in credits(adapter)] == ["7"]


@pytest.mark.asyncio
async def test_invitation_gate_and_uniform_credit_lookup(setup):
    service, adapter, roster, mapping, now, _ = setup
    caller = await enable_rewards(setup)
    await configure(setup)
    await service.command(caller, "5", "抽奖邀请门槛", "1")
    await service.command(caller, "5", "开启抽奖", "")
    user = event(adapter, "2", private=False)
    assert "当前为0" in await service.command(user, "5", "参加抽奖", "")
    roster["6"] = member("6")
    mapping["6"] = "2"
    now[0] += 36
    await service.tick(adapter)
    assert await service.command(user, "5", "参加抽奖", "") == "参加抽奖成功。"


@pytest.mark.asyncio
async def test_historical_results_and_settings_remain_queryable(setup):
    service, adapter, _, _, _, _ = setup
    caller = await configure(setup)
    settings = await service.command(caller, "5", "抽奖设置", "")
    assert "18元猪脚饭" in settings and "中奖人数：3" in settings
    await service.command(caller, "5", "开启抽奖", "")
    await service.command(event(adapter, "2", private=False), "5", "参加抽奖", "")
    await service.command(caller, "5", "立即开奖", "")
    await service.command(caller, "5", "抽奖奖励", "下一期奖品")
    await service.command(event(adapter, mid="next"), "5", "开启抽奖", "")
    records = await service.command(caller, "5", "抽奖记录", "")
    assert "第1期" in records and "第2期" in records
    result = await service.command(caller, "5", "中奖名单", "1")
    assert "18元猪脚饭" in result and "Member 2" in result
    assert "尚未开奖" in await service.command(caller, "5", "中奖名单", "")


@pytest.mark.asyncio
async def test_old_or_unverified_signup_does_not_enroll(setup):
    service, adapter, _, _, _, _ = setup
    caller = await configure(setup)
    await service.command(caller, "5", "开启抽奖", "")
    user = event(adapter, "2", private=False)
    for payload in [
        {},
        {"created_at": 0},
        {"created_at": 999000},
        {"created_at": "1000000"},
    ]:
        user.get_extra = lambda _, p=payload: p
        assert "已过期" in await service.command(user, "5", "参加抽奖", "")
    with closing(activity_store.database(adapter)) as db:
        assert db.execute("SELECT COUNT(*) FROM lottery_entries").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("elapsed", [1, 360, 899])
async def test_mobile_signup_uses_transport_time_throughout_fifteen_minutes(setup, elapsed):
    from astrbot.core.platform.sources.wangshangliao.adapter import message_timestamp

    service, adapter, _, _, now, _ = setup
    caller = await configure(setup)
    await service.command(caller, "5", "抽奖倒计时", "15")
    await service.command(caller, "5", "开启抽奖", "")
    now[0] += elapsed
    user = event(adapter, "2", private=False)
    stamp = message_timestamp(SimpleNamespace(created_at=0), {7: str(int(now[0] * 1000))})
    user.get_extra = lambda _: {"created_at": stamp}
    assert await service.command(user, "5", "参加抽奖", "") == "参加抽奖成功。"
    assert await service.command(user, "5", "参加抽奖", "") == "你已参加本次抽奖，请勿重复报名。"


@pytest.mark.asyncio
async def test_mobile_historical_signup_cannot_use_receive_time(setup):
    from astrbot.core.platform.sources.wangshangliao.adapter import message_timestamp

    service, adapter, _, _, now, _ = setup
    caller = await configure(setup)
    await service.command(caller, "5", "抽奖倒计时", "15")
    await service.command(caller, "5", "开启抽奖", "")
    now[0] += 600
    user = event(adapter, "2", private=False)
    for server_time in ["999000", "1001000", "1606000"]:
        stamp = message_timestamp(SimpleNamespace(created_at=0), {7: server_time})
        user.get_extra = lambda _, value=stamp: {"created_at": value}
        assert "已过期" in await service.command(user, "5", "参加抽奖", "")
    now[0] = 1900
    assert "没有开放报名" in await service.command(user, "5", "参加抽奖", "")


@pytest.mark.asyncio
async def test_lottery_cancellation_notice_is_kept(setup):
    service, adapter, _, _, _, _ = setup
    caller = await configure(setup)
    await service.command(caller, "5", "开启抽奖", "")
    assert "已取消" in await service.command(caller, "5", "取消抽奖", "")
    assert adapter.send_reply_text.await_args.kwargs["auto_recall"] is False


@pytest.mark.asyncio
async def test_lottery_status_closes_registration_before_worker_draw(setup):
    service, adapter, _, _, now, _ = setup
    caller = await configure(setup)
    await service.command(caller, "5", "开启抽奖", "")
    user = event(adapter, "2", private=False)
    assert "报名中" in await service.command(user, "5", "抽奖状态", "")
    now[0] += 60
    reply = await service.command(user, "5", "抽奖状态", "")
    assert "报名已截止，等待开奖" in reply and "剩余：0 秒" in reply
    assert "报名中" not in reply
    assert "没有开放报名" in await service.command(user, "5", "参加抽奖", "")
    assert latest(adapter)["status"] == "open"
    await service.tick(adapter)
    assert "已开奖" in await service.command(user, "5", "抽奖状态", "")


@pytest.mark.asyncio
async def test_signup_rechecks_deadline_after_slow_member_lookup(setup):
    service, adapter, roster, _, now, _ = setup
    caller = await configure(setup)
    await service.command(caller, "5", "开启抽奖", "")
    now[0] += 59
    user = event(adapter, "2", private=False)
    user.get_extra = lambda _: {"created_at": 1059000}

    async def slow_roster(_group):
        now[0] += 2
        return {"complete": True, "groupMemberInfo": list(roster.values())}

    adapter.get_moderation_members.side_effect = slow_roster
    assert await service.command(user, "5", "参加抽奖", "") == "报名已截止。"
    with closing(activity_store.database(adapter)) as db:
        assert db.execute("SELECT COUNT(*) FROM lottery_entries").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_new_account_and_group_never_see_old_rewards_or_lotteries(setup):
    service, adapter, _, _, _, config = setup
    await enable_rewards(setup)
    caller = await configure(setup)
    await service.command(caller, "5", "开启抽奖", "")
    assert "尚未开启" in await service.command(event(adapter), "6", "抽奖状态", "")
    adapter.account = "11"
    result = await service.command(event(adapter), "5", "抽奖状态", "")
    assert result == "尚未开启抽奖。"
    assert "0.00" in await service.command(
        event(adapter, "2", private=False), "5", "我的邀请", ""
    )


@pytest.mark.asyncio
async def test_revoked_reward_owner_pauses_without_awarding(setup):
    service, adapter, roster, mapping, now, config = setup
    await enable_rewards(setup)
    roster["6"] = member("6")
    mapping["6"] = "2"
    config["admins_id"] = []
    now[0] += 36
    await service.tick(adapter)
    with closing(activity_store.database(adapter)) as db:
        assert db.execute("SELECT enabled FROM invite_rules").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM invite_credits").fetchone()[0] == 0
    adapter.get_group_inviters.assert_not_awaited()


@pytest.mark.asyncio
async def test_identity_change_after_invitation_read_never_credits(setup):
    service, adapter, roster, mapping, now, _ = setup
    await enable_rewards(setup)
    roster["6"] = member("6")
    mapping["6"] = "2"
    original = adapter.get_group_inviters.side_effect

    async def changed(group, *, members):
        result = await original(group, members=members)
        adapter.account = "11"
        return result

    adapter.get_group_inviters.side_effect = changed
    now[0] += 36
    await service.tick(adapter)
    with closing(activity_store.database(adapter)) as db:
        assert db.execute("SELECT COUNT(*) FROM invite_credits").fetchone()[0] == 0


@pytest.mark.parametrize(
    "text",
    [
        "参加抽奖",
        "抽奖状态",
        "我的邀请",
        "邀请奖励",
        "开启抽奖",
        "立即开奖",
        "取消抽奖",
        "中奖名单",
        "抽奖奖励 18元猪脚饭",
        "中奖人数 3",
        "抽奖倒计时 10",
        "参与上限 15",
        "抽奖邀请门槛 3",
        "领奖联系人 秦铭",
        "设置邀请奖励 5.25",
        "开启邀请奖励",
        "邀请奖励状态",
        "邀请记录",
        "暂停邀请奖励",
    ],
)
def test_activity_commands_are_exact_bare_commands(text):
    assert recognize_command(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "有人说参加抽奖",
        "参加抽奖吗",
        "不开启抽奖",
        "参加抽奖 2",
        "我的邀请 别人",
        "中奖名单\n开启抽奖",
        "抽奖奖励",
        "设置邀请奖励",
    ],
)
def test_activity_keywords_in_chat_do_not_execute(text):
    assert recognize_command(text) == ""
