"""Offline, actor-bound rules-only upgrades with no billing or model side effects."""

import asyncio
import copy
import json
import os
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope
from tests.test_wangshangliao_rules_provisioning import invoke as provision
from tests.test_wangshangliao_rules_provisioning import prepare


async def rules_only(scope, group="5"):
    """Provision an isolated native-admin group through the existing public API."""
    value, _, _ = scope
    payload = await prepare(scope, group)
    preview = await provision(value, payload)
    await provision(
        value, {**payload, "confirmation": preview["confirmation"]}, commit=True
    )
    row = value.store.tenant("99", group)
    return {"account": "99", "group_id": group, "version": row["version"]}


async def invoke(value, payload, *, commit=False, actor="dashboard/offline"):
    """Call only the explicit server upgrade actions, never live transport."""
    return await OpenRPC(value).dispatch(
        OpenCall(
            action="ai_admission" if commit else "ai_admission_preview",
            actor=actor,
            payload=payload,
        )
    )


@pytest.mark.asyncio
async def test_upgrade_preserves_tenant_rules_billing_and_disabled_ai(rpc_scope):
    value, adapter, _ = rpc_scope
    payload = await rules_only(rpc_scope)
    before = copy.deepcopy(value.store.tenants())
    ai_before = copy.deepcopy(value.group_ai.configs["99", "5"])
    with value.store.connect() as db:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT IN ('tenants','capabilities','audit','actor_bindings')"
            )
        }
        saved = {
            table: list(db.execute(f'SELECT * FROM "{table}"')) for table in tables
        }
    preview = await invoke(value, payload)
    assert preview["mode"] == "full" and preview["expires_in"] == 600
    assert value.store.tenants() == before
    result = await invoke(
        value, {**payload, "confirmation": preview["confirmation"]}, commit=True
    )
    assert result == {"saved": True, "mode": "full", "version": payload["version"] + 1}
    after = value.store.tenant("99", "5")
    old = next(
        row for row in before if (row["account"], row["group_id"]) == ("99", "5")
    )
    assert after == {**old, "version": old["version"] + 1, "rules_only": 0}
    assert [
        row
        for row in value.store.tenants()
        if (row["account"], row["group_id"]) != ("99", "5")
    ] == [row for row in before if (row["account"], row["group_id"]) != ("99", "5")]
    assert value.tenants["99", "5"] == after
    assert value.group_ai.configs["99", "5"] == ai_before
    assert not value.group_ai.ready(adapter, value.group_ai.configs["99", "5"])
    with value.store.connect() as db:
        assert {
            table: list(db.execute(f'SELECT * FROM "{table}"')) for table in tables
        } == saved
        assert (
            db.execute(
                "SELECT used FROM capabilities WHERE kind='ai_admission'"
            ).fetchone()[0]
            == 1
        )
        assert (
            json.loads(
                db.execute(
                    "SELECT detail FROM audit WHERE action='ai_admission_upgraded'"
                ).fetchone()[0]
            )["mode"]
            == "full"
        )
    with pytest.raises(OpenError, match="tenant_changed"):
        await invoke(
            value, {**payload, "confirmation": preview["confirmation"]}, commit=True
        )


@pytest.mark.asyncio
async def test_existing_payer_nomination_never_becomes_self_consent(rpc_scope):
    value, _, _ = rpc_scope
    payload = await rules_only(rpc_scope)
    await OpenRPC(value).dispatch(
        OpenCall(
            action="payer_nominate",
            actor="dashboard/offline",
            payload={
                "account": "99",
                "group_id": "5",
                "tenant_version": payload["version"],
                "uid": "1",
                "version": 0,
                "pool_version": value.plans.snapshot("nim/1001", "1")["version"],
            },
        )
    )
    before = value.plans.payer("99", "5")
    assert not before["confirmed"]
    preview = await invoke(value, payload)
    await invoke(
        value, {**payload, "confirmation": preview["confirmation"]}, commit=True
    )
    assert value.plans.payer("99", "5") == before
    with pytest.raises(OpenError, match="billing_payer"):
        value.plans.group_pool("99", "5")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "actor",
        "group",
        "expired",
        "role",
        "peer",
        "bot_role",
        "version",
        "epoch",
        "rules",
        "paused",
        "service_paused",
        "settings_version",
        "departed",
        "blocked",
        "reconciling",
    ],
)
async def test_upgrade_preview_is_revoked_by_scope_or_authority_change(
    rpc_scope, change
):
    value, _, rows = rpc_scope
    payload = await rules_only(rpc_scope)
    other = await rules_only(rpc_scope, "6")
    preview = await invoke(value, payload)
    actor = "dashboard/other" if change == "actor" else "dashboard/offline"
    if change == "group":
        payload = other
    elif change in {"role", "bot_role"}:
        rows[1 if change == "role" else 0]["groupRole"] = "GROUP_ROLE_MEMBER"
    elif change == "peer":
        rows[1]["nimId"] = "9001"
    elif change in {"service_paused", "settings_version"}:
        value.store.configure(
            value.store.settings()["version"],
            change == "service_paused",
            20,
            1001,
            "dashboard/offline",
        )
        await value.refresh()
    elif change == "blocked":
        value.suspended_groups.add(("99", "5"))
    elif change == "reconciling":
        value.reconciling_groups["99", "5"] = 1
    elif change not in {"actor", "group"}:
        with value.store.connect(write=True) as db:
            if change == "expired":
                db.execute("UPDATE capabilities SET expires=?", (time.time() - 1,))
            else:
                sql = {
                    "version": "version=version+1",
                    "epoch": "epoch=epoch+1",
                    "rules": 'rules=\'{"advertising":false,"flood":true,"keywords":[]}\'',
                    "paused": "paused=1",
                    "departed": "present=0",
                }[change]
                db.execute(
                    f"UPDATE tenants SET {sql} WHERE account='99' AND group_id='5'"
                )
        await value.refresh()
    with pytest.raises((OpenError, ProtocolError)):
        await invoke(
            value,
            {**payload, "confirmation": preview["confirmation"]},
            commit=True,
            actor=actor,
        )
    assert value.store.tenant("99", "5")["rules_only"] == 1
    assert value.store.tenant("99", "6")["rules_only"] == 1
    with value.store.connect() as db:
        token = db.execute(
            "SELECT used FROM capabilities WHERE kind='ai_admission'"
        ).fetchone()
        assert token is None if change == "bot_role" else token["used"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"rules_only": False},
        {"confirmed": True},
        {"config": {"enabled": True}},
        {"managers": ["2"]},
        {"version": True},
        {"account": "199"},
        {"version": 0},
    ],
)
async def test_upgrade_has_no_arbitrary_config_or_billing_payload(rpc_scope, extra):
    value, _, _ = rpc_scope
    payload = await rules_only(rpc_scope)
    with pytest.raises((ValidationError, OpenError)):
        await invoke(value, {**payload, **extra})
    assert value.store.tenant("99", "5")["rules_only"]


@pytest.mark.asyncio
async def test_upgrade_requires_server_and_an_existing_rules_only_tenant(rpc_scope):
    value, _, _ = rpc_scope
    payload = await rules_only(rpc_scope)
    with pytest.raises(OpenError, match="server_required"):
        await invoke(value, payload, actor="1")
    with pytest.raises(OpenError, match="ai_admission_unavailable"):
        await invoke(
            value,
            {
                "account": "99",
                "group_id": "6",
                "version": value.store.tenant("99", "6")["version"],
            },
        )


@pytest.mark.asyncio
async def test_revocation_at_commit_rolls_back_tenant_and_confirmation(
    rpc_scope, monkeypatch
):
    value, _, _ = rpc_scope
    payload = await rules_only(rpc_scope)
    before = value.store.tenant("99", "5")
    preview = await invoke(value, payload)
    log = value.store.log

    def revoke(db, actor, account, group, action, detail=None):
        log(db, actor, account, group, action, detail)
        if action == "ai_admission_upgraded":
            value.member_evidence["99", "5"]["1"]["groupRole"] = "GROUP_ROLE_MEMBER"

    monkeypatch.setattr(value.store, "log", revoke)
    with pytest.raises(OpenError, match="manager_native_role_required"):
        await invoke(
            value, {**payload, "confirmation": preview["confirmation"]}, commit=True
        )
    assert value.store.tenant("99", "5") == before
    with value.store.connect() as db:
        assert (
            db.execute(
                "SELECT used FROM capabilities WHERE kind='ai_admission'"
            ).fetchone()[0]
            == 0
        )
        assert not db.execute(
            "SELECT 1 FROM audit WHERE action='ai_admission_upgraded'"
        ).fetchone()


@pytest.mark.asyncio
async def test_inconsistent_old_ai_switches_cannot_start_ai_on_upgrade(rpc_scope):
    value, _, _ = rpc_scope
    payload = await rules_only(rpc_scope)
    with value.store.connect(write=True) as db:
        config = {
            **value.group_ai.configs["99", "5"]["config"],
            "enabled": True,
            "mentions": True,
        }
        db.execute(
            "UPDATE group_ai SET config=? WHERE account='99' AND group_id='5'",
            (json.dumps(config),),
        )
    preview = await invoke(value, payload)
    await invoke(
        value, {**payload, "confirmation": preview["confirmation"]}, commit=True
    )
    assert not any(
        value.group_ai.configs["99", "5"]["config"][mode]
        for mode in ("enabled", "mentions", "autonomous", "idle")
    )


@pytest.mark.asyncio
async def test_cancelled_upgrade_drains_and_reconciles_before_return(
    rpc_scope, monkeypatch
):
    value, _, _ = rpc_scope
    payload = await rules_only(rpc_scope)
    preview = await invoke(value, payload)
    started, release = threading.Event(), threading.Event()
    log = value.store.log

    def pause(db, actor, account, group, action, detail=None):
        log(db, actor, account, group, action, detail)
        if action == "ai_admission_upgraded":
            started.set()
            assert release.wait(5)

    monkeypatch.setattr(value.store, "log", pause)
    task = asyncio.create_task(
        invoke(
            value,
            {**payload, "confirmation": preview["confirmation"]},
            commit=True,
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert value.store.tenant("99", "5")["rules_only"]
    assert value.tenants["99", "5"] == value.store.tenant("99", "5")
    assert not value.reconciling_groups
    with value.store.connect() as db:
        assert (
            db.execute(
                "SELECT used FROM capabilities WHERE kind='ai_admission'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WSL_AI_ADMISSION_PAGE"),
    reason="Opt-in compiled management-page test",
)
async def test_upgrade_browser_confirmation_cancel_and_mobile_layout(
    rpc_scope, tmp_path
):
    """Exercise the compiled page against the isolated real RPC implementation."""
    from playwright.async_api import async_playwright, expect

    from scripts.check_wsl_policy_page import FIXTURE

    value, _, _ = rpc_scope
    calls = []
    rpc = OpenRPC(value)

    async def api(_source, body):
        calls.append(copy.deepcopy(body))
        return await rpc.dispatch(OpenCall(actor="dashboard/offline", **body))

    bridge = (
        FIXTURE
        + """
    const get = window.AstrBotPluginPage.apiGet;
    window.AstrBotPluginPage.apiGet = async (endpoint, ...args) => {
      const value = await get(endpoint, ...args);
      return endpoint === "settings" ? {...value, open_mode:"integrated"} : value;
    };
    window.AstrBotPluginPage.apiPost = async (endpoint, body) => {
      if (endpoint !== "open-service") throw new Error("Unexpected test endpoint");
      return window.admissionAPI(body);
    };
    """
    )
    asset = Path(os.environ["WSL_AI_ADMISSION_PAGE"]).resolve()
    output = Path(os.environ.get("WSL_AI_ADMISSION_OUTPUT", str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(args=["--no-sandbox"])
        try:
            for width in (320, 390, 1440):
                await rules_only(rpc_scope)
                page = await browser.new_page(viewport={"width": width, "height": 900})
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                await page.expose_binding("admissionAPI", api)
                await page.add_init_script(bridge)
                await page.goto(asset.as_uri())
                await page.get_by_role("tab", name="开通", exact=True).click()
                scope = page.locator(".open-service")
                await scope.get_by_label("群聊", exact=True).select_option("99/5")
                preview = scope.get_by_role("button", name="预览升级正式AI", exact=True)
                await expect(preview).to_be_enabled()
                await preview.click()
                dialog = scope.get_by_role("alertdialog", name="确认正式AI准入")
                await expect(dialog).to_be_visible()
                assert value.store.tenant("99", "5")["rules_only"] == 1
                await dialog.get_by_role("button", name="取消", exact=True).click()
                await expect(dialog).to_have_count(0)
                assert not any(call["action"] == "ai_admission" for call in calls)
                await preview.click()
                await expect(dialog).to_be_visible()
                await page.screenshot(path=str(output / f"ai-admission-{width}.png"))
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                assert await dialog.evaluate(
                    """element => [...element.querySelectorAll('button')].every(
                      button => button.scrollWidth <= button.clientWidth)"""
                )
                await dialog.get_by_role("button", name="确认升级", exact=True).click()
                await expect(
                    scope.get_by_role("status").filter(has_text="正式AI准入已确认")
                ).to_be_visible()
                assert value.store.tenant("99", "5")["rules_only"] == 0
                assert not value.group_ai.configs["99", "5"]["config"]["enabled"]
                await expect(preview).to_have_count(0)
                await page.get_by_role("tab", name="AI服务", exact=True).click()
                await page.get_by_role("tab", name="群聊与热群", exact=True).click()
                await expect(
                    scope.get_by_label("启用本群AI", exact=True)
                ).not_to_be_checked()
                assert not errors, errors
                calls.clear()
                await page.close()
        finally:
            await browser.close()
