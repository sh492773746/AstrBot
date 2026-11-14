"""Fixed-tier member reservations stay scoped, frozen and deterministic."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from astrbot.builtin_stars.wangshangliao_moderation import activity_store, dashboard
from astrbot.builtin_stars.wangshangliao_moderation.lottery_rules import (
    validate_reserved_members,
)
from tests.test_wangshangliao_activities import configure, event, latest
from tests.test_wangshangliao_activities import setup as setup
from tests.test_wangshangliao_dashboard import PREFIX
from tests.test_wangshangliao_dashboard import headers as headers
from tests.test_wangshangliao_dashboard import page_app as page_app


def reservation(member="2", peer="902", amount=5000):
    return {
        "member": member,
        "peer": peer,
        "name": f"Member {member}",
        "amount": amount,
    }


def test_reservation_validation_enforces_unique_members_and_tier_capacity():
    tiers = [{"amount": 5000, "count": 1}, {"amount": 1000, "count": 2}]
    assert validate_reserved_members([reservation()], tiers)[0]["amount"] == 5000
    with pytest.raises(ValueError, match="只能指定一次"):
        validate_reserved_members(
            [reservation(), reservation(peer="903", amount=1000)], tiers
        )
    with pytest.raises(ValueError, match="不能超过"):
        validate_reserved_members([reservation(), reservation("3", "903", 5000)], tiers)


@pytest.mark.asyncio
async def test_reserved_member_receives_selected_tier_and_plan_is_frozen(setup):
    service, adapter, _, _, now, _ = setup
    caller = await configure(setup, capacity=3)
    reserved = [reservation("3", "903", 5000)]
    with activity_store.database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET total_amount=10000,packet_mode='tiered',"
            "capacity=3,winners=3,prize_tiers=?,reserved_members=?",
            (
                json.dumps([{"amount": 5000, "count": 1}]),
                json.dumps(reserved),
            ),
        )
    assert "抽奖已开启" in await service.command(caller, "5", "开启抽奖", "")
    opened = latest(adapter)
    assert json.loads(opened["reserved_members"]) == reserved
    for uid in ("2", "3", "4"):
        assert "参加抽奖成功" in await service.command(
            event(adapter, uid, private=False), "5", "参加抽奖", ""
        )
    with activity_store.database(adapter) as db:
        db.execute("UPDATE lottery_settings SET reserved_members='[]'")
    now[0] += 61
    await service.tick(adapter)
    row = latest(adapter)
    winners = {winner["uid"]: winner for winner in json.loads(row["results"])}
    assert row["status"] == "finished"
    assert winners["3"]["amount"] == 5000
    assert sum(winner["amount"] for winner in winners.values()) == 10000
    assert row["reserved_members"] == opened["reserved_members"]


@pytest.mark.asyncio
async def test_missing_reserved_member_blocks_without_falling_back(setup):
    service, adapter, _, _, now, _ = setup
    caller = await configure(setup, capacity=3)
    with activity_store.database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET total_amount=10000,packet_mode='tiered',"
            "capacity=3,winners=3,prize_tiers=?,reserved_members=?",
            (
                json.dumps([{"amount": 5000, "count": 1}]),
                json.dumps([reservation("4", "904", 5000)]),
            ),
        )
    await service.command(caller, "5", "开启抽奖", "")
    for uid in ("2", "3"):
        await service.command(event(adapter, uid, private=False), "5", "参加抽奖", "")
    now[0] += 61
    await service.tick(adapter)
    row = latest(adapter)
    assert row["status"] == "blocked"
    assert row["error"] == "lottery_reserved_member_unavailable"
    assert row["results"] == "[]"
    assert adapter.send_reply_text.await_count == 1


@pytest.mark.asyncio
async def test_reserved_member_is_selected_before_random_capacity_fill(setup):
    service, adapter, _, _, now, _ = setup
    caller = await configure(setup, capacity=2)
    with activity_store.database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET total_amount=10000,packet_mode='tiered',"
            "capacity=2,winners=2,prize_tiers=?,reserved_members=?",
            (
                json.dumps([{"amount": 5000, "count": 1}]),
                json.dumps([reservation("4", "904", 5000)]),
            ),
        )
    await service.command(caller, "5", "开启抽奖", "")
    for uid in ("2", "4"):
        await service.command(event(adapter, uid, private=False), "5", "参加抽奖", "")
    opened = latest(adapter)
    with activity_store.database(adapter) as db:
        db.execute(
            "INSERT INTO lottery_entries(lottery,member,peer,name,joined) "
            "VALUES(?,?,?,?,?)",
            (opened["id"], "3", "903", "Member 3", now[0]),
        )
    now[0] += 61
    await service.tick(adapter)
    row = latest(adapter)
    winners = {winner["uid"]: winner for winner in json.loads(row["results"])}
    assert row["status"] == "finished"
    assert len(winners) == 2
    assert winners["4"]["amount"] == 5000


@pytest.mark.asyncio
async def test_dashboard_searches_and_saves_verified_reservation(page_app, headers):
    app, page, _, _, root = page_app
    roster = {
        "2": {
            "userId": "2",
            "nimId": "102",
            "groupMemberNick": "指定群员",
            "accountState": "ACCOUNT_STATE_GOOD",
            "groupRole": "GROUP_ROLE_MEMBER",
        }
    }
    page.plugin = SimpleNamespace(
        open_service=SimpleNamespace(active=True, roster=AsyncMock(return_value=roster))
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        search = await client.get(
            f"{PREFIX}/lottery-members",
            params={"bot_id": "fixture", "group_id": "5", "query": "指定"},
            headers=headers,
        )
        assert search.status_code == 200
        assert search.json()["data"]["members"] == [
            {"member": "2", "peer": "102", "name": "指定群员"}
        ]
        current = (
            await client.get(
                f"{PREFIX}/activities",
                params={"bot_id": "fixture", "group_id": "5"},
                headers=headers,
            )
        ).json()["data"]
        lottery = {
            **current["lottery"],
            "prize": "分档测试",
            "contact": "管理员",
            "packet_mode": "tiered",
            "total_amount": "100.00",
            "capacity": 3,
            "winners": 3,
            "prize_tiers": [{"amount": "50.00", "count": 1}],
            "reserved_members": [{"member": "2", "peer": "102", "amount": "50.00"}],
        }
        saved = await client.post(
            f"{PREFIX}/activities",
            headers=headers,
            json={
                "bot_id": "fixture",
                "group_id": "5",
                "revision": current["revision"],
                "lottery": lottery,
            },
        )
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["lottery"]["reserved_members"] == [
        {"member": "2", "peer": "102", "name": "指定群员", "amount": "50.00"}
    ]
    with sqlite3.connect(root / "activities.sqlite3") as db:
        stored = json.loads(
            db.execute("SELECT reserved_members FROM lottery_settings").fetchone()[0]
        )
    assert stored == [reservation("2", "102", 5000) | {"name": "指定群员"}]


@pytest.mark.asyncio
async def test_exact_member_search_precedes_broad_name_matches(
    page_app, headers, monkeypatch
):
    app, page, _, _, _ = page_app
    monkeypatch.setattr(dashboard, "is_managed_account", lambda _: False)
    roster = {
        str(uid): {
            "nimId": str(uid + 1000),
            "groupMemberNick": "Member 2",
            "accountState": "ACCOUNT_STATE_GOOD",
        }
        for uid in [*range(10, 40), 2]
    }
    page.plugin = SimpleNamespace(
        open_service=SimpleNamespace(active=True, roster=AsyncMock(return_value=roster))
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get(
            f"{PREFIX}/lottery-members",
            params={"bot_id": "fixture", "group_id": "5", "query": "2"},
            headers=headers,
        )
    assert response.status_code == 200
    assert response.json()["data"]["members"] == [
        {"member": "2", "peer": "1002", "name": "Member 2"}
    ]


@pytest.mark.asyncio
async def test_managed_accounts_cannot_be_reserved(page_app, headers, monkeypatch):
    app, page, _, _, _ = page_app
    monkeypatch.setattr(dashboard, "is_managed_account", lambda uid: uid == "2")
    roster = {
        "2": {"nimId": "102", "userNick": "Bot", "accountState": "ACCOUNT_STATE_GOOD"}
    }
    page.plugin = SimpleNamespace(
        open_service=SimpleNamespace(active=True, roster=AsyncMock(return_value=roster))
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers=headers,
    ) as client:
        search = await client.get(
            f"{PREFIX}/lottery-members",
            params={"bot_id": "fixture", "group_id": "5", "query": "Bot"},
        )
        assert search.json()["data"]["members"] == []
        current = (
            await client.get(
                f"{PREFIX}/activities", params={"bot_id": "fixture", "group_id": "5"}
            )
        ).json()["data"]
        saved = await client.post(
            f"{PREFIX}/activities",
            json={
                "bot_id": "fixture",
                "group_id": "5",
                "revision": current["revision"],
                "lottery": {
                    **current["lottery"],
                    "prize": "Test",
                    "packet_mode": "tiered",
                    "total_amount": "100",
                    "capacity": 3,
                    "prize_tiers": [{"amount": "50", "count": 1}],
                    "reserved_members": [
                        {"member": "2", "peer": "102", "amount": "50"}
                    ],
                },
            },
        )
    assert saved.status_code == 409
    assert "机器人" in saved.json()["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["search", "save"])
async def test_member_lookup_timeout_never_saves_old_evidence(
    page_app, headers, monkeypatch, operation
):
    app, page, _, _, _ = page_app
    monkeypatch.setattr(dashboard, "LOTTERY_MEMBER_LOOKUP_SECONDS", 0.01)
    cancelled = asyncio.Event()

    async def unavailable(*_):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    page.plugin = SimpleNamespace(
        open_service=SimpleNamespace(
            active=True, roster=AsyncMock(side_effect=unavailable)
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers=headers,
    ) as client:
        params = {"bot_id": "fixture", "group_id": "5"}
        before = (await client.get(f"{PREFIX}/activities", params=params)).json()[
            "data"
        ]
        if operation == "search":
            response = await client.get(
                f"{PREFIX}/lottery-members", params={**params, "query": "Member"}
            )
        else:
            response = await client.post(
                f"{PREFIX}/activities",
                json={
                    **params,
                    "revision": before["revision"],
                    "lottery": {
                        **before["lottery"],
                        "prize": "Test",
                        "packet_mode": "tiered",
                        "total_amount": "100",
                        "capacity": 3,
                        "prize_tiers": [{"amount": "50", "count": 1}],
                        "reserved_members": [
                            {"member": "2", "peer": "102", "amount": "50"}
                        ],
                    },
                },
            )
        assert response.status_code == 503
        assert "超时" in response.json()["message"]
        assert cancelled.is_set()
        after = (await client.get(f"{PREFIX}/activities", params=params)).json()["data"]
    assert before == after


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["notice_unknown", "result_unknown"])
async def test_unknown_publication_keeps_activity_settings_frozen(
    page_app, headers, status
):
    app, _, service, _, _ = page_app
    adapter = service.list_bots()["bots"][0]
    native = page_app[1]._adapter("fixture")
    with activity_store.database(native) as db:
        db.execute(
            "INSERT INTO lotteries(id,account,group_id,status,created) "
            "VALUES('unknown',?,?,?,1)",
            (str(adapter["account_id"]), "5", status),
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers=headers,
    ) as client:
        params = {"bot_id": "fixture", "group_id": "5"}
        current = (await client.get(f"{PREFIX}/activities", params=params)).json()[
            "data"
        ]
        response = await client.post(
            f"{PREFIX}/activities",
            json={
                **params,
                "revision": current["revision"],
                "lottery": {**current["lottery"], "prize": "Changed"},
            },
        )
    assert response.status_code == 409


def test_current_lottery_snapshot_uses_one_query_without_exposing_internal_fields(
    page_app, monkeypatch
):
    _, page, _, configs, _ = page_app
    adapter = page._adapter("fixture")
    with activity_store.database(adapter) as db:
        db.execute(
            "INSERT INTO lotteries(id,account,group_id,status,created,owner,command,"
            "packet_mode,reserved_members) "
            "VALUES('snapshot','1','5','finished',1,'private-owner','private-command',"
            "'tiered',?)",
            (
                json.dumps(
                    [
                        {
                            "member": "2",
                            "peer": "102",
                            "name": "Private member",
                            "amount": 5000,
                        }
                    ]
                ),
            ),
        )
    queries = []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(queries.append)
        return db

    monkeypatch.setattr(dashboard.sqlite3, "connect", traced)
    state = dashboard.DashboardPage._activity_snapshot(adapter, configs["fixture"], "5")
    assert len([query for query in queries if "FROM lotteries" in query]) == 1
    assert state["current_lottery"]["id"] == "snapshot"
    assert not {
        "owner",
        "command",
        "packet_amounts",
        "results",
        "account",
        "session",
        "reserved_members",
    }.intersection(state["current_lottery"])
