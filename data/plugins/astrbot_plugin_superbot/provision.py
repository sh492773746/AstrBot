"""Idempotent isolated AstrBot profile provisioning after identity is supplied."""

import copy
import hashlib
from pathlib import Path

from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.provider.provider import EmbeddingProvider, Provider

from .store import Rejected


async def prepare(
    context,
    store,
    platform_id,
    chat_provider,
    embedding_provider,
    chat_only_tenant=False,
):
    """Create only this bot's persona, reviewed knowledge and configuration route.

    Args:
        context: Running AstrBot context.
        store: Isolated plugin database.
        platform_id: Explicit dedicated Telegram instance identifier.
        chat_provider: Existing configured chat provider identifier.
        embedding_provider: Existing configured embedding provider identifier.

    Returns:
        The dedicated configuration ID.
    """
    if chat_only_tenant:
        return await prepare_chat_only(context, store, platform_id, chat_provider)
    providers_ready = isinstance(
        context.get_provider_by_id(chat_provider), Provider
    ) and isinstance(context.get_provider_by_id(embedding_provider), EmbeddingProvider)
    suffix = hashlib.sha256(platform_id.encode()).hexdigest()[:12]
    name = "大海传媒超级机器人-" + suffix
    route = f"{platform_id}:*:*"
    previous = context.astrbot_config_mgr.ucr.umop_to_conf_id.get(route)
    saved = store.get("profile_id")
    if previous and previous != saved:
        raise Rejected("目标平台已有其他配置路由，拒绝覆盖；请确认使用新机器人")
    if not providers_ready:
        # AstrBot may initialize plugins before provider instances. Existing
        # deterministic business must not depend on model startup ordering.
        manager = context.astrbot_config_mgr
        kb_name = name + "-使用知识"
        if not saved or saved not in manager.confs:
            raise Rejected("首次配置需要有效聊天和嵌入模型 ID")
        conf = manager.confs[saved]
        bound = conf.get("kb_names") or []
        if (
            conf.get("admins_id")
            or kb_name not in bound
            or not set(bound) <= {kb_name, "大海传媒超级机器人-独立客服知识库"}
        ):
            raise Rejected("专用配置权限或知识库绑定已改变，请先核查")
        await manager.ucr.update_route(route, saved)
        return saved
    root = Path(__file__).parent / "docs"
    persona_id = "superbot-" + suffix
    persona = context.persona_manager.get_persona_v3_by_id(persona_id)
    if persona is None:
        await context.persona_manager.create_persona(
            persona_id, (root / "persona.md").read_text(), tools=[], skills=[]
        )
    kb_name = name + "-使用知识"
    kb = await context.kb_manager.get_kb_by_name(kb_name)
    if kb is None:
        kb = await context.kb_manager.create_kb(
            kb_name,
            description="仅本机器人已核实使用说明；不包含其他机器人资料",
            embedding_provider_id=embedding_provider,
        )
    documents = await kb.kb_db.list_documents_by_kb(kb.kb.kb_id)
    names = {doc.doc_name for doc in documents}
    current_names = set()
    for filename in ("player-help.md", "player-services.md"):
        content = (root / filename).read_bytes()
        file_name = (
            "superbot-" + hashlib.sha256(content).hexdigest()[:12] + "-" + filename
        )
        if file_name not in names:
            await kb.upload_document(file_name, content, "md")
        current_names.add(file_name)
    # Index every replacement before retiring official manuals or the old stub.
    # Preserve independently supplied customer documents.
    for document in documents:
        if document.doc_name in current_names:
            continue
        if any(
            document.doc_name == filename
            or (
                document.doc_name.startswith("superbot-")
                and document.doc_name.endswith("-" + filename)
            )
            for filename in ("player-help.md", "player-services.md", "player-game.md")
        ):
            await kb.delete_document(document.doc_id)
    manager = context.astrbot_config_mgr
    if saved:
        if saved not in manager.confs:
            raise Rejected("专用配置已被删除，请按维护文档恢复，不能静默替换")
        conf = manager.confs[saved]
    else:
        # Recover a profile whose creation succeeded before its ID was recorded.
        matches = [c for c in manager.get_conf_list() if c["name"] == name]
        if len(matches) > 1:
            raise Rejected("存在重复专用配置，请人工核查")
        if matches:
            saved = matches[0]["id"]
        else:
            config = copy.deepcopy(DEFAULT_CONFIG)
            config["admins_id"] = []
            config["plugin_set"] = ["astrbot_plugin_superbot"]
            config["wake_prefix"] = []
            config["platform_settings"]["friend_message_needs_wake_prefix"] = False
            config["platform_settings"]["ignore_bot_self_message"] = True
            config["provider_settings"]["persona_pool"] = [persona_id]
            config["agent_runner"]["config"]["model"]["provider_id"] = chat_provider
            config["agent_runner"]["config"]["persona"]["persona_id"] = persona_id
            config["kb_names"] = [kb_name]
            config["kb_agentic_mode"] = False
            saved = await manager.create_conf(config, name=name)
        with store.tx() as db:
            store.put(db, "profile_id", saved)
            store.audit(
                db,
                "system",
                "profile_created",
                {"id": saved, "kb": kb_name, "persona": persona_id},
            )
        conf = manager.confs[saved]
    allowed_knowledge = {kb_name, "大海传媒超级机器人-独立客服知识库"}
    bound_knowledge = conf.get("kb_names") or []
    if (
        conf.get("admins_id")
        or kb_name not in bound_knowledge
        or not set(bound_knowledge) <= allowed_knowledge
    ):
        raise Rejected("专用配置权限或知识库绑定已改变，请先核查")
    await manager.ucr.update_route(route, saved)
    return saved


async def prepare_chat_only(context, store, platform_id, chat_provider):
    """Provision tenant chat without uploading documents or requiring embeddings."""
    if not platform_id.startswith("tenant-"):
        raise Rejected("仅租户实例支持无知识库模式")
    manager = context.astrbot_config_mgr
    route = f"{platform_id}:*:*"
    suffix = hashlib.sha256(platform_id.encode()).hexdigest()[:12]
    name = "tenant-chat-" + suffix
    saved = store.get("profile_id")
    previous = manager.ucr.umop_to_conf_id.get(route)
    if previous and previous != saved:
        raise Rejected("目标平台已有其他配置路由")
    if not saved:
        matches = [c for c in manager.get_conf_list() if c["name"] == name]
        if len(matches) > 1:
            raise Rejected("存在重复租户配置")
        if matches:
            saved = matches[0]["id"]
        else:
            config = copy.deepcopy(DEFAULT_CONFIG)
            config["admins_id"] = []
            config["plugin_set"] = [
                "astrbot_plugin_superbot",
                "astrbot_plugin_tenant_health",
            ]
            config["disable_builtin_commands"] = True
            config["wake_prefix"] = []
            config["platform_settings"]["friend_message_needs_wake_prefix"] = False
            config["provider_settings"]["enable"] = True
            config["provider_settings"]["proactive_capability"]["add_cron_tools"] = (
                False
            )
            config["agent_runner"]["config"]["model"]["provider_id"] = chat_provider
            config["kb_names"] = []
            config["kb_agentic_mode"] = False
            config["dashboard"]["enable"] = False
            saved = await manager.create_conf(config, name=name)
        with store.tx() as db:
            store.put(db, "profile_id", saved)
            store.audit(db, "system", "chat_only_profile_created", {"id": saved})
    if saved not in manager.confs:
        raise Rejected("租户配置已被删除")
    conf = manager.confs[saved]
    if (
        conf.get("admins_id")
        or conf.get("kb_names")
        or not conf.get("disable_builtin_commands")
    ):
        raise Rejected("租户配置权限或知识库绑定已改变")
    await manager.ucr.update_route(route, saved)
    return saved
