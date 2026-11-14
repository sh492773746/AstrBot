"""Offline private-storage removal with group and accounting preservation."""

import inspect
import json
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.ai_plans import AIPlans
from astrbot.builtin_stars.wangshangliao_moderation.conversation_profiles import (
    DEFAULT,
    ConversationProfiles,
)
from astrbot.builtin_stars.wangshangliao_moderation.knowledge_store import (
    KnowledgeStore,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_store import (
    DEFAULT_RULES,
    OpenError,
    OpenStore,
)
from scripts.cleanup_wsl_private import (
    INTERACTION_TABLES,
    PRIVATE_TABLES,
    cleanup,
    main,
    private_origin,
)

PLATFORM = "wangshangliao_exact_1"
PRIVATE = PLATFORM + ":FriendMessage:99/nim/1001/wsl-ai/99/customer"
GROUP = PLATFORM + ":GroupMessage:99/5/wsl-ai/99/group"


def snapshot(path, *, exclude=()):
    """Read deterministic logical table contents for fixture-only comparisons.

    Args:
        path: Isolated SQLite file.
        exclude: Explicit tables intentionally removed by the test.

    Returns:
        Table names mapped to unchanged schema and row values.
    """
    with closing(sqlite3.connect(path)) as db:
        return {
            name: (
                schema,
                db.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall(),
            )
            for name, schema in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            if name not in exclude
        }


@pytest.fixture
def accounting(tmp_path):
    store = OpenStore(tmp_path)
    store.discover("99", {"5": {"name": "offline-group", "team": "1005"}})
    store.import_group("99", "5", DEFAULT_RULES, ["1"], "offline")
    tenant = store.tenant("99", "5")
    store.native_role("99", "5", tenant["epoch"], "GROUP_ROLE_ADMIN")
    plans = AIPlans(store)
    service = SimpleNamespace(
        store=store,
        context=SimpleNamespace(),
        integrated=False,
        model={},
        resolve_model=lambda _: {"model": "offline", "api_key": "never-send"},
    )
    profiles = ConversationProfiles(service)
    return store, plans, profiles


def test_fresh_and_reopened_stores_never_create_private_tables(accounting):
    store, plans, _ = accounting
    assert not set(PRIVATE_TABLES) & snapshot(store.path).keys()
    assert "support_owner" not in inspect.signature(store.activate).parameters
    assert "guidance_trial" not in inspect.signature(plans.reserve).parameters
    for name in ("queue_notices", "claim_notice", "finish_notice"):
        assert not hasattr(plans, name)
    assert "notices" not in plans.status()
    AIPlans(OpenStore(store.path.parent))
    assert not set(PRIVATE_TABLES) & snapshot(store.path).keys()
    assert {"actor_bindings", "account_labels", "membership_notices"} <= snapshot(
        store.path
    ).keys()


@pytest.mark.parametrize("kind", ["consultation", "admin_guidance", "private", None])
def test_private_profiles_are_rejected_before_storage(accounting, monkeypatch, kind):
    store, _, profiles = accounting
    connect = Mock(side_effect=AssertionError("no storage for a retired profile"))
    monkeypatch.setattr(store, "connect", connect)
    with pytest.raises(OpenError, match="ai_profile_invalid"):
        profiles.resolve(kind)
    with pytest.raises(OpenError, match="ai_profile_invalid"):
        profiles.save(kind, "", "", 0, DEFAULT, "offline")
    connect.assert_not_called()


def test_group_profile_bindings_limits_inventory_and_cas_remain(accounting):
    store, _, profiles = accounting
    profiles.save(
        "group",
        "",
        "",
        0,
        {**DEFAULT, "max_tokens": 4096, "history_turns": 20},
        "offline",
        rebind=True,
    )
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO ai_profiles VALUES('consultation','99','',1,'NOT JSON')"
        )
        db.execute(
            "INSERT INTO ai_profiles VALUES('admin_guidance','','',1,'NOT JSON')"
        )
    default = profiles.resolve("group")
    assert default["parameters"]["max_completion_tokens"] == 512
    assert default["history_turns"] == 12
    assert [row["kind"] for row in profiles.rows()] == ["group"]
    assert profiles.inventory() == {"configs": [], "personas": []}
    tenant = store.tenant("99", "5")
    profiles.save(
        "group",
        "99",
        "5",
        0,
        {**DEFAULT, "max_tokens": 120},
        "1",
        tenant_version=tenant["version"],
    )
    assert profiles.resolve("group", "99", "5")["parameters"] == {
        "max_completion_tokens": 120
    }
    with pytest.raises(OpenError, match="ai_profile_changed"):
        profiles.save(
            "group",
            "99",
            "5",
            0,
            DEFAULT,
            "1",
            tenant_version=tenant["version"],
        )
    profiles.save(
        "group",
        "99",
        "5",
        1,
        None,
        "1",
        tenant_version=tenant["version"],
    )
    assert profiles.resolve("group", "99", "5")["parameters"] == default["parameters"]


def test_activation_has_no_support_import_or_side_effect(tmp_path):
    store = OpenStore(tmp_path)
    store.discover("99", {"5": {"name": "offline", "team": "1005"}})
    token = store.mint("99", "5", "dashboard/offline")
    tenant = store.tenant("99", "5")
    store.activate(
        "99", "5", "1", code=token, epoch=tenant["epoch"], version=tenant["version"]
    )
    assert store.tenant("99", "5")["active"]
    assert store.tenant("99", "5")["managers"] == ["1"]
    assert not set(PRIVATE_TABLES) & snapshot(store.path).keys()


def test_group_billing_reserve_settle_quota_and_unknown_hold_survive(accounting):
    store, plans, _ = accounting
    model = {
        "model": "offline",
        "base_url": "https://offline.invalid",
        "api_key": "fake",
    }
    payload = {
        "model": "offline",
        "max_completion_tokens": 128,
        "messages": [{"role": "user", "content": "group question"}],
    }
    plans.configure(1, "offline", "offline", "1000", "offline")
    plans.configure_price(
        store.model_binding(model),
        "offline",
        0,
        {
            "input_per_million": "0.1",
            "output_per_million": "0.2",
            "multiplier": "1",
            "credit_cny": "1",
            "input_ceiling": 10000,
            "output_ceiling": 1024,
            "all_output_billed_in_limit": True,
            "evidence": "offline test",
        },
        "offline",
    )
    tenant = store.tenant("99", "5")
    pool = plans.snapshot("nim/1001", "1")
    plans.bind(
        "99",
        "5",
        "1",
        "nim/1001",
        0,
        tenant["version"],
        pool["version"],
        "1",
        guard=lambda: None,
    )
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO group_ai VALUES('99','5',1,?,'',0,'')",
            (json.dumps({"enabled": True, "daily_limit": 2}),),
        )
    now = time.time()
    quote = plans.quote(model, payload)
    plans.reserve(
        "nim/1001",
        "1",
        "settled-group",
        quote,
        "99",
        "5",
        payer_version=1,
        group_version=1,
        now=now,
    )
    plans.dispatched("settled-group", quote, payload)
    plans.settle(
        "settled-group",
        quote,
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )
    plans.finish("settled-group", "completed")
    plans.reserve(
        "nim/1001",
        "1",
        "unknown-group",
        quote,
        "99",
        "5",
        payer_version=1,
        group_version=1,
        now=now + 61,
    )
    plans.finish("unknown-group", "unknown")
    with store.connect(write=True) as db:
        db.execute("CREATE TABLE chat_history(account,sender,messages,updated)")
        db.execute("INSERT INTO chat_history VALUES('99','nim/1001','private',0)")
    before = snapshot(store.path, exclude=("chat_history",))
    targets = [("open", store.path)]
    preview = cleanup(targets)
    cleanup(targets, apply=True, offline=True, expect_digest=preview["digest"])
    assert snapshot(store.path) == before
    reopened = AIPlans(OpenStore(store.path.parent))
    assert reopened.snapshot("nim/1001", "1", now=now + 122)["used"] == 2
    assert reopened.group_pool("99", "5")["uid"] == "1"
    with pytest.raises(OpenError, match="group_quota"):
        reopened.reserve(
            "nim/1001",
            "1",
            "over-quota",
            quote,
            "99",
            "5",
            payer_version=1,
            group_version=1,
            now=now + 122,
        )
    reopened.prune(now=now + 100 * 86400)
    with store.connect() as db:
        unknown = db.execute(
            "SELECT state,charged_micro FROM ai_charges WHERE request='unknown-group'"
        ).fetchone()
    assert tuple(unknown) == ("unknown", quote["reserved_micro"])


def test_private_capability_removed_without_touching_group_confirmations(accounting):
    store, _, _ = accounting
    with store.connect(write=True) as db:
        db.executemany(
            "INSERT INTO capabilities(digest,kind) VALUES(?,?)",
            [
                ("private", "guidance_trial"),
                ("group", "activation"),
                ("future", "unknown"),
            ],
        )
    targets = [("open", store.path)]
    preview = cleanup(targets)
    cleanup(targets, apply=True, offline=True, expect_digest=preview["digest"])
    with store.connect() as db:
        assert [
            tuple(row)
            for row in db.execute(
                "SELECT digest,kind FROM capabilities ORDER BY digest"
            )
        ] == [("future", "unknown"), ("group", "activation")]


def test_maintenance_does_not_access_private_storage(accounting, monkeypatch):
    store, plans, _ = accounting
    with store.connect(write=True) as db:
        db.execute("CREATE TABLE chat_history(account,sender,messages,updated)")
        db.execute("INSERT INTO chat_history VALUES('99','nim/1001','old',0)")
    legacy = snapshot(store.path)["chat_history"]
    store.prune(now=1)
    plans.prune(now=1)
    assert snapshot(store.path)["chat_history"] == legacy
    assert "guidance_trial" not in inspect.getsource(AIPlans)
    assert "chat_history" not in inspect.getsource(OpenStore)


@pytest.mark.parametrize(
    "origin,match",
    [
        (PRIVATE, True),
        (PLATFORM + ":FriendMessage:99/nim/1001", True),
        (GROUP, False),
        ("telegram:FriendMessage:99", False),
        (PLATFORM + "_other:FriendMessage:99", False),
        (PLATFORM + ":FriendMessage:", False),
        (PLATFORM + ":GroupMessage:contains:FriendMessage:99", False),
        ("prefix:" + PRIVATE, False),
        (None, False),
    ],
)
def test_private_origin_requires_exact_platform_and_message_type(origin, match):
    assert private_origin(origin, (PLATFORM,)) is match


def test_preview_apply_is_exact_idempotent_and_preserves_accounting(accounting):
    store, plans, profiles = accounting
    profiles.save("group", "", "", 0, DEFAULT, "offline")
    with store.connect(write=True) as db:
        for table, columns in PRIVATE_TABLES.items():
            ordered = sorted(columns)
            definition = ",".join(f'"{column}" TEXT' for column in ordered)
            db.execute(f'CREATE TABLE "{table}"({definition})')
            placeholders = ",".join("?" for _ in ordered)
            db.execute(
                f'INSERT INTO "{table}" VALUES({placeholders})',
                ("PRIVATE_MESSAGE_CANARY",) * len(ordered),
            )
        db.executemany(
            "INSERT INTO ai_profiles VALUES(?,?,?,?,?)",
            [
                ("consultation", "99", "", 1, "PRIVATE_MESSAGE_CANARY"),
                ("admin_guidance", "", "", 1, "PRIVATE_MESSAGE_CANARY"),
                ("future_kind", "", "", 1, "future_binding"),
            ],
        )
        db.execute("INSERT INTO actor_bindings VALUES('99','1','1001',1,0)")
        db.execute("INSERT INTO account_labels VALUES('99','999','group-bot',1)")
        db.execute("INSERT INTO ai_pools VALUES('uid/1','1','nim/1001',1)")
        db.execute(
            "INSERT INTO ai_subscriptions VALUES("
            "'paid','uid/1','plus',1,2,1000000,700000,1,'offline')"
        )
        for group, state in (("", "unknown"), ("5", "unknown"), ("", "settled")):
            key = group + state
            db.execute(
                "INSERT INTO ai_charges(request,pool,period,day,month,account,"
                "group_id,uid,principal,binding,price_version,reserved_micro,"
                "charged_micro,state,created) VALUES("
                "?, 'uid/1','paid','2026-01-01','2026-01','99',?,'1','nim/1001',"
                "'offline',1,100,100,?,1)",
                (key, group, state),
            )
    before = snapshot(store.path, exclude=(*PRIVATE_TABLES, "ai_profiles"))
    targets = [("open", store.path)]
    preview = cleanup(targets)
    assert not preview["applied"]
    assert "PRIVATE_MESSAGE_CANARY" not in json.dumps(preview)
    assert snapshot(store.path, exclude=(*PRIVATE_TABLES, "ai_profiles")) == before
    assert set(PRIVATE_TABLES) <= snapshot(store.path).keys()
    applied = cleanup(
        targets,
        apply=True,
        expect_digest=preview["digest"],
        offline=True,
    )
    assert applied["applied"] and applied["digest"] == preview["digest"]
    assert snapshot(store.path, exclude=("ai_profiles",)) == before
    with store.connect() as db:
        assert [
            row[0] for row in db.execute("SELECT kind FROM ai_profiles ORDER BY kind")
        ] == ["future_kind", "group"]
    assert cleanup(targets)["databases"][0]["actions"] == []
    profiles.resolve("group")
    AIPlans(OpenStore(store.path.parent))
    assert snapshot(store.path, exclude=("ai_profiles",)) == before
    # Maintenance no longer queries removed guidance/private history tables.
    plans.prune(now=100 * 86400)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM ai_charges").fetchone()[0] == 3


@pytest.fixture
def verified_private_ai_db(tmp_path):
    path = tmp_path / "open-service.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(
            "CREATE TABLE private_ai_sources(account,principal,message_id,batch_id);"
            "INSERT INTO private_ai_sources VALUES('99','nim/1','m1','b1');"
            "CREATE TABLE private_ai_batches("
            "id,account,principal,messages,state,created,updated);"
            "INSERT INTO private_ai_batches VALUES("
            "'b1','99','nim/1','PRIVATE_CANARY','unknown',1,2);"
            "CREATE TABLE chat_history(account,sender,messages,updated);"
            "INSERT INTO chat_history VALUES('99','nim/1','PRIVATE_CANARY',2);"
            "CREATE TABLE ai_charges(request,state,charged_micro);"
            "INSERT INTO ai_charges VALUES('b1','unknown',123);"
            "CREATE TABLE group_ai(account,group_id,config);"
            "INSERT INTO group_ai VALUES('99','5','keep');"
        )
    return path


def test_verified_private_ai_schemas_preview_and_apply_preserve_shared_data(
    verified_private_ai_db,
):
    path = verified_private_ai_db
    before = snapshot(path)
    targets = [("open", path)]
    preview = cleanup(targets)
    assert snapshot(path) == before
    assert "PRIVATE_CANARY" not in json.dumps(preview)
    assert {
        action["table"]: (action["operation"], action["count"])
        for action in preview["databases"][0]["actions"]
    } == {
        "private_ai_sources": ("drop_private_table", 1),
        "private_ai_batches": ("drop_private_table", 1),
        "chat_history": ("drop_private_table", 1),
    }
    cleanup(targets, apply=True, offline=True, expect_digest=preview["digest"])
    assert snapshot(path) == {
        table: contents
        for table, contents in before.items()
        if table not in {"private_ai_sources", "private_ai_batches", "chat_history"}
    }


@pytest.mark.parametrize(
    "table", ["private_ai_sources", "private_ai_batches", "chat_history"]
)
@pytest.mark.parametrize("defect", ["missing_column", "extra_column", "old_definition"])
def test_private_ai_schema_variants_refuse_without_changes(
    verified_private_ai_db, table, defect
):
    path = verified_private_ai_db
    with closing(sqlite3.connect(path)) as db, db:
        if defect == "missing_column":
            column = "message_id" if table == "private_ai_sources" else "updated"
            db.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
        elif defect == "extra_column":
            db.execute(f'ALTER TABLE "{table}" ADD COLUMN future_shared TEXT')
        else:
            db.execute(f'DROP TABLE "{table}"')
            columns = {
                "private_ai_sources": "source,batch",
                "private_ai_batches": "id,state,updated",
                "chat_history": "account,sender,messages",
            }[table]
            db.execute(f'CREATE TABLE "{table}"({columns})')
    before = snapshot(path)
    with pytest.raises(ValueError, match=f"unrecognized_schema:{table}") as exc:
        cleanup([("open", path)])
    assert "missing=" in str(exc.value) and "unexpected=" in str(exc.value)
    if defect == "extra_column":
        assert "missing=[]" in str(exc.value)
        assert "unexpected=['future_shared']" in str(exc.value)
    else:
        assert "missing=[]" not in str(exc.value)
    assert snapshot(path) == before


def test_inbox_cleanup_never_matches_group_payload_or_other_routes(tmp_path):
    path = tmp_path / "messages.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(
            "CREATE TABLE inbox(account TEXT,team TEXT,message TEXT,payload TEXT);"
            "CREATE TABLE outbox(key TEXT,state TEXT);"
            "INSERT INTO outbox VALUES('private/13/peer','unknown');"
        )
        for index, team in enumerate(
            [
                "private/13/peer",
                "1005",
                "unsupported/1/team",
                "privateish/13",
                "private/",
            ]
        ):
            db.execute(
                "INSERT INTO inbox VALUES('99',?,?,?)",
                (
                    team,
                    str(index),
                    json.dumps({"text": PRIVATE, "team": "private/13/peer"}),
                ),
            )
    targets = [("messages", path)]
    preview = cleanup(targets)
    assert preview["databases"][0]["actions"][0]["count"] == 1
    cleanup(targets, apply=True, offline=True, expect_digest=preview["digest"])
    rows = snapshot(path)
    assert len(rows["inbox"][1]) == 4
    assert rows["outbox"][1] == [("private/13/peer", "unknown")]


def test_mixed_interaction_database_keeps_group_onboarding(tmp_path):
    path = tmp_path / "private_interaction.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        for table, columns in INTERACTION_TABLES.items():
            db.execute(f'CREATE TABLE "{table}"({",".join(sorted(columns))})')
        db.executescript(
            "CREATE TABLE group_onboarding(account,group_id,epoch,created,outcome);"
            "INSERT INTO group_onboarding VALUES('99','5',1,1,'accepted');"
            "CREATE TABLE future_shared(value);"
            "INSERT INTO future_shared VALUES('keep');"
        )
    before = snapshot(path, exclude=INTERACTION_TABLES)
    targets = [("interaction", path)]
    preview = cleanup(targets)
    cleanup(targets, apply=True, offline=True, expect_digest=preview["digest"])
    assert snapshot(path) == before
    assert path.is_file()


@pytest.fixture
def friend_approval_paths(tmp_path):
    paths = []
    for index in range(3):
        root = tmp_path / f"friend-{index}"
        root.mkdir()
        path = root / "friend_approval.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript(
                "CREATE TABLE approval_settings("
                "account TEXT PRIMARY KEY,enabled INTEGER,version INTEGER,paused INTEGER,"
                "last_scan REAL,last_action REAL,pending TEXT,retry_at REAL,"
                "read_failures INTEGER,last_request TEXT,welcome_initialized INTEGER,"
                "welcome_checked_at REAL);"
                "CREATE TABLE approval_attempts("
                "account TEXT,request_id TEXT,member TEXT,state TEXT,created REAL,"
                "updated REAL,PRIMARY KEY(account,request_id));"
                "CREATE TABLE welcome_observations("
                "account TEXT,request_id TEXT,member TEXT,peer TEXT,state TEXT,"
                "PRIMARY KEY(account,request_id));"
                "CREATE TABLE group_onboarding(account,group_id,outcome);"
                "INSERT INTO group_onboarding VALUES('99','5','accepted');"
                "CREATE TABLE ai_charges(request,state,charged_micro);"
                "INSERT INTO ai_charges VALUES('hold','unknown',123);"
                "CREATE TABLE chat_history(value);"
                "INSERT INTO chat_history VALUES('other_role_table');"
                "CREATE TABLE future_shared(value);"
                "INSERT INTO future_shared VALUES('keep');"
            )
            db.executemany(
                "INSERT INTO approval_settings VALUES(?,1,1,0,1,1,?,0,0,'',1,1)",
                [(str(number), "PRIVATE_CANARY") for number in range(55)],
            )
            db.executemany(
                "INSERT INTO approval_attempts VALUES('99',?,'1','unknown',1,1)",
                [(str(number),) for number in range(34)],
            )
            db.execute(
                "INSERT INTO welcome_observations VALUES("
                "'99','welcome','1','peer','accepted')"
            )
        paths.append(path)
    return paths


def test_friend_approval_preview_apply_only_drops_verified_tables(
    friend_approval_paths,
):
    targets = [("friend_approval", path) for path in friend_approval_paths]
    before = [snapshot(path) for path in friend_approval_paths]
    preview = cleanup(targets)
    assert not preview["applied"]
    assert [snapshot(path) for path in friend_approval_paths] == before
    assert "PRIVATE_CANARY" not in json.dumps(preview)
    totals = {"approval_settings": 0, "approval_attempts": 0, "welcome_observations": 0}
    for database in preview["databases"]:
        assert {action["table"] for action in database["actions"]} == set(totals)
        for action in database["actions"]:
            assert action["operation"] == "drop_private_table"
            totals[action["table"]] += action["count"]
    assert totals == {
        "approval_settings": 165,
        "approval_attempts": 102,
        "welcome_observations": 3,
    }
    cleanup(targets, apply=True, offline=True, expect_digest=preview["digest"])
    for path, original in zip(friend_approval_paths, before):
        assert path.is_file()
        assert snapshot(path) == {
            name: contents for name, contents in original.items() if name not in totals
        }
    repeated = cleanup(targets)
    assert all(not database["actions"] for database in repeated["databases"])
    cleanup(targets, apply=True, offline=True, expect_digest=repeated["digest"])


@pytest.mark.parametrize(
    "table",
    ["approval_settings", "approval_attempts", "welcome_observations"],
)
@pytest.mark.parametrize("defect", ["missing_column", "extra_column"])
def test_friend_approval_schema_drift_refuses_all_databases(
    friend_approval_paths,
    table,
    defect,
):
    path = friend_approval_paths[-1]
    with closing(sqlite3.connect(path)) as db, db:
        if defect == "missing_column":
            db.execute(
                f'ALTER TABLE "{table}" RENAME COLUMN account TO unknown_account'
            )
        else:
            db.execute(f'ALTER TABLE "{table}" ADD COLUMN future_shared TEXT')
    before = [snapshot(path) for path in friend_approval_paths]
    with pytest.raises(ValueError, match=f"unrecognized_schema:{table}"):
        cleanup([("friend_approval", path) for path in friend_approval_paths])
    assert [snapshot(path) for path in friend_approval_paths] == before


@pytest.mark.parametrize("danger", ["trigger", "foreign_key"])
def test_friend_approval_rejects_trigger_or_foreign_key_side_effects(
    friend_approval_paths,
    danger,
):
    path = friend_approval_paths[0]
    with closing(sqlite3.connect(path)) as db, db:
        if danger == "trigger":
            db.executescript(
                "CREATE TRIGGER unsafe AFTER DELETE ON approval_settings "
                "BEGIN DELETE FROM future_shared; END;"
            )
        else:
            db.executescript(
                "CREATE TABLE shared_receipts(account REFERENCES approval_settings(account) "
                "ON DELETE CASCADE); INSERT INTO shared_receipts VALUES('0');"
            )
    before = snapshot(path)
    with pytest.raises(ValueError, match=f"unsafe_target_{danger}"):
        cleanup([("friend_approval", path)])
    assert snapshot(path) == before


def test_friend_approval_cli_supports_repeated_explicit_paths(
    friend_approval_paths,
    capsys,
):
    before = [snapshot(path) for path in friend_approval_paths]
    argv = [
        argument
        for path in friend_approval_paths
        for argument in ("--friend-approval-db", str(path))
    ]
    assert main(argv) == 0
    preview = json.loads(capsys.readouterr().out)
    assert not preview["applied"]
    assert len(preview["databases"]) == 3
    assert {database["kind"] for database in preview["databases"]} == {
        "friend_approval"
    }
    assert [snapshot(path) for path in friend_approval_paths] == before
    with pytest.raises(SystemExit) as exc:
        main([*argv, "--apply", "--expect-digest", preview["digest"]])
    assert exc.value.code == 2
    assert [snapshot(path) for path in friend_approval_paths] == before


def test_friend_approval_requires_exact_filename_and_matching_preview(
    friend_approval_paths,
):
    path = friend_approval_paths[0]
    with pytest.raises(ValueError, match="database_scope_invalid"):
        cleanup([("messages", path)])
    targets = [("friend_approval", path)]
    preview = cleanup(targets)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE approval_settings SET version=version+1 WHERE account='0'")
    before = snapshot(path)
    with pytest.raises(ValueError, match="preview_changed"):
        cleanup(targets, apply=True, offline=True, expect_digest=preview["digest"])
    assert snapshot(path) == before
    other = path.with_name("messages.sqlite3")
    path.rename(other)
    with pytest.raises(ValueError, match="database_scope_invalid"):
        cleanup([("friend_approval", other)])


@pytest.fixture
def legacy_knowledge(accounting):
    store, _, _ = accounting
    KnowledgeStore(store)
    with store.connect(write=True) as db:
        for number, kind in enumerate(("personal", "group", "future", "personal"), 1):
            db.execute(
                "INSERT INTO knowledge_entries VALUES(?,?,?,'99','5',?,?,?,1,1)",
                (
                    number,
                    kind,
                    "owner",
                    f"title-{number}",
                    f"body-{number}",
                    number * 10,
                ),
            )
            # One missing legacy index row must not broaden the deletion.
            if number != 4:
                db.execute(
                    "INSERT INTO knowledge_fts(rowid,tokens) VALUES(?,?)",
                    (number, f"token{number}"),
                )
        db.executemany(
            "INSERT INTO knowledge_spaces VALUES(?,'owner','99','5',1,1,1)",
            [("personal",), ("group",), ("future",)],
        )
        db.execute("INSERT INTO knowledge_fts(rowid,tokens) VALUES(999,'orphan')")
        db.execute(
            "INSERT INTO knowledge_operations VALUES('private-request','digest','audit',1)"
        )
    return store


def test_personal_knowledge_cleanup_preserves_group_fts_totals_and_audit(
    legacy_knowledge,
):
    store = legacy_knowledge
    before = snapshot(store.path)
    targets = [("open", store.path)]
    preview = cleanup(targets)
    assert snapshot(store.path) == before
    counts = {
        action["table"]: action["count"]
        for action in preview["databases"][0]["actions"]
    }
    assert counts == {
        "knowledge_entries": 2,
        "knowledge_spaces": 1,
        "knowledge_fts": 1,
    }
    assert preview["databases"][0]["actions"][0]["table"] == "knowledge_fts"
    cleanup(targets, apply=True, offline=True, expect_digest=preview["digest"])
    after = snapshot(store.path)
    for table in before:
        if table not in {
            "knowledge_entries",
            "knowledge_spaces",
            "knowledge_totals",
        } and not table.startswith("knowledge_fts"):
            assert after[table] == before[table]
    assert after["knowledge_entries"][1] == before["knowledge_entries"][1][1:3]
    assert after["knowledge_spaces"][1] == [
        row for row in before["knowledge_spaces"][1] if row[0] != "personal"
    ]
    assert after["knowledge_totals"][1] == [(1, 50, 2)]
    with store.connect() as db:
        assert [
            tuple(row)
            for row in db.execute(
                "SELECT rowid,tokens FROM knowledge_fts ORDER BY rowid"
            )
        ] == [(2, "token2"), (3, "token3"), (999, "orphan")]
        assert [
            row[0]
            for row in db.execute(
                "SELECT rowid FROM knowledge_fts WHERE knowledge_fts MATCH 'token2'"
            )
        ] == [2]
        assert not db.execute(
            "SELECT rowid FROM knowledge_fts WHERE knowledge_fts MATCH 'token1'"
        ).fetchall()
    assert cleanup(targets)["databases"][0]["actions"] == []
    KnowledgeStore(OpenStore(store.path.parent))
    assert snapshot(store.path) == after


@pytest.mark.parametrize(
    "defect", ["missing_trigger", "changed_trigger", "totals", "cascade"]
)
def test_personal_knowledge_schema_drift_refuses_without_group_changes(
    legacy_knowledge,
    defect,
):
    store = legacy_knowledge
    with store.connect(write=True) as db:
        if defect in {"missing_trigger", "changed_trigger"}:
            db.execute("DROP TRIGGER knowledge_total_delete")
            if defect == "changed_trigger":
                db.executescript(
                    "CREATE TRIGGER knowledge_total_delete AFTER DELETE ON knowledge_entries "
                    "BEGIN DELETE FROM calls; END;"
                )
        elif defect == "totals":
            db.execute("UPDATE knowledge_totals SET bytes=bytes+1")
        else:
            db.executescript(
                "CREATE TRIGGER unknown_totals AFTER UPDATE ON knowledge_totals "
                "BEGIN DELETE FROM calls; END;"
            )
    before = snapshot(store.path)
    with pytest.raises(ValueError, match="knowledge"):
        cleanup([("open", store.path)])
    assert snapshot(store.path) == before


def test_framework_cleanup_uses_origin_only_and_keeps_audit(tmp_path):
    path = tmp_path / "framework.db"
    with closing(sqlite3.connect(path)) as db, db:
        for table, field in (
            ("conversations", "user_id"),
            ("platform_message_history", "user_id"),
            ("platform_sessions", "session_id"),
            ("umo_aliases", "umo"),
            ("session_project_relations", "session_id"),
            ("webchat_threads", "parent_session_id"),
            ("provider_stats", "umo"),
            ("financial_audit", "umo"),
        ):
            db.execute(f'CREATE TABLE "{table}"({field} TEXT,content TEXT)')
            db.executemany(
                f'INSERT INTO "{table}" VALUES(?,?)',
                [
                    (PRIVATE, "private"),
                    (GROUP, PRIVATE),
                    ("telegram:FriendMessage:99", PRIVATE),
                ],
            )
        db.executescript("CREATE TABLE preferences(scope,scope_id,key,value);")
        db.executemany(
            "INSERT INTO preferences VALUES(?,?,?,?)",
            [
                ("umo", PRIVATE, "sel_conv_id", "delete"),
                ("umo", GROUP, "sel_conv_id", PRIVATE),
                ("global", PRIVATE, "finance", "keep"),
            ],
        )
    before = snapshot(path)
    targets = [("framework", path)]
    preview = cleanup(targets, platform_ids=(PLATFORM,))
    cleanup(
        targets,
        platform_ids=(PLATFORM,),
        apply=True,
        offline=True,
        expect_digest=preview["digest"],
    )
    after = snapshot(path)
    for table in before:
        if table in {"provider_stats", "financial_audit"}:
            assert after[table] == before[table]
        else:
            assert len(after[table][1]) == 2
            assert after[table][1] == [
                row
                for row in before[table][1]
                if not (
                    row[0] == "umo" and row[1] == PRIVATE
                    if table == "preferences"
                    else row[0] == PRIVATE
                )
            ]


@pytest.mark.parametrize("offline,digest", [(False, ""), (True, ""), (False, "x")])
def test_apply_requires_offline_and_digest(tmp_path, offline, digest):
    path = tmp_path / "messages.sqlite3"
    with pytest.raises(ValueError, match="apply_requires"):
        cleanup(
            [("messages", path)],
            apply=True,
            offline=offline,
            expect_digest=digest,
        )
    assert not path.exists()


def test_changed_preview_refuses_every_database_before_any_write(tmp_path):
    targets = []
    for directory in ("first", "second"):
        root = tmp_path / directory
        root.mkdir()
        path = root / "messages.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript(
                "CREATE TABLE inbox(account,team,message,payload);"
                "INSERT INTO inbox VALUES('99','private/1/peer','1','old');"
            )
        targets.append(("messages", path))
    preview = cleanup(targets)
    with closing(sqlite3.connect(targets[1][1])) as db, db:
        db.execute("UPDATE inbox SET payload='changed'")
    before = [snapshot(path) for _, path in targets]
    with pytest.raises(ValueError, match="preview_changed"):
        cleanup(targets, apply=True, offline=True, expect_digest=preview["digest"])
    assert [snapshot(path) for _, path in targets] == before


@pytest.mark.parametrize("danger", ["trigger", "foreign_key", "schema"])
def test_cleanup_refuses_side_effects_and_unfamiliar_schema(tmp_path, danger):
    path = tmp_path / "messages.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(
            "CREATE TABLE inbox(account,team,message PRIMARY KEY,payload);"
            "INSERT INTO inbox VALUES('99','private/1/peer','1','private');"
            "CREATE TABLE protected_group(value);"
            "INSERT INTO protected_group VALUES('keep');"
        )
        if danger == "trigger":
            db.executescript(
                "CREATE TRIGGER unsafe AFTER DELETE ON inbox BEGIN "
                "DELETE FROM protected_group; END;"
            )
        elif danger == "foreign_key":
            db.executescript(
                "CREATE TABLE receipts(message REFERENCES inbox(message) "
                "ON DELETE CASCADE); INSERT INTO receipts VALUES('1');"
            )
        else:
            db.execute("ALTER TABLE inbox RENAME COLUMN payload TO unknown_payload")
    before = snapshot(path)
    with pytest.raises(ValueError):
        cleanup([("messages", path)])
    assert snapshot(path) == before


def test_no_discovery_creation_or_unsafe_path_scope(tmp_path):
    path = tmp_path / "messages.sqlite3"
    with pytest.raises(FileNotFoundError):
        cleanup([("messages", path)])
    assert not path.exists()
    with closing(sqlite3.connect(path)):
        pass
    alias = tmp_path / "alias.sqlite3"
    alias.symlink_to(path)
    with pytest.raises(ValueError, match="symlink"):
        cleanup([("framework", alias)], platform_ids=(PLATFORM,))
    with pytest.raises(ValueError, match="explicit_unique"):
        cleanup([("messages", path), ("messages", path)])
    with pytest.raises(ValueError, match="database_scope"):
        cleanup([("framework", path)])
    with pytest.raises(ValueError, match="database_scope"):
        cleanup([("open", path)])


def test_cli_defaults_to_preview_and_requires_explicit_scope(tmp_path, capsys):
    path = tmp_path / "messages.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(
            "CREATE TABLE inbox(account,team,message,payload);"
            "INSERT INTO inbox VALUES('99','private/1/peer','1','PRIVATE_CANARY');"
        )
    assert main(["--messages-db", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert not result["applied"]
    assert len(snapshot(path)["inbox"][1]) == 1
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
