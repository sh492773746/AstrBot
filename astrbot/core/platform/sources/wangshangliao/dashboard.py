"""Native Dashboard operations, separate from shared platform services."""

import asyncio
import copy

from astrbot.core import logger


async def save_configuration(
    service, config: dict, owner: str, current: dict | None = None, *, persist
) -> None:
    """Commit a native login reference with rollback on configuration failure.

    Args:
        service: Existing configuration service and platform lifecycle.
        config: Public platform settings and optional one-time session reference.
        owner: Authenticated Dashboard principal.
        current: Existing configuration when editing or logging in again.
        persist: Existing core configuration writer, including its failure behavior.

    Raises:
        ValueError: On invalid fields, identity conflicts or expired references.
    """
    import hashlib

    from .registration import (
        registrations,
    )
    from .runtime_policy import (
        BUSINESS_FIELDS,
        runtime,
    )
    from .storage import Vault

    config = copy.deepcopy(config)
    expected = config.pop("_business_revision", "")
    config.pop("_business_active", None)
    config.pop("_business_epoch", None)
    business_change = None
    if current and runtime.snapshot(current):
        saved_business = runtime.snapshot(current).values()
        proposed_business = {
            **saved_business,
            **{key: config[key] for key in BUSINESS_FIELDS if key in config},
        }
        if proposed_business != saved_business:
            if not expected or not runtime.service:
                raise ValueError("请在旺商聊插件管理页修改业务设置")
            business_change = proposed_business
        for key in BUSINESS_FIELDS:
            config.pop(key, None)

    allowed = {
        "id",
        "type",
        "enable",
        "account_id",
        "nickname",
        "enabled_groups",
        "reply_private",
        "reply_groups",
        "ai_routes",
        "proactive_send",
        "moderation",
        "developer_test",
        "session_ref",
        "login_method",
        "name",
    }
    if set(config) - allowed or config.get("type") != "wangshangliao":
        raise ValueError("platform_config_fields")
    if config.get("login_method", "password") not in {"password", "sms"}:
        raise ValueError("login_method_invalid")
    groups = config.get("enabled_groups", [])
    from .policy import validate_policy

    if type(config.get("reply_private", True)) is not bool:
        raise ValueError("reply_private_invalid")
    replies = config.get("reply_groups", {})
    if not isinstance(replies, dict) or any(
        not isinstance(group, str)
        or not group.isascii()
        or not group.isdigit()
        or int(group) <= 0
        or type(enabled) is not bool
        for group, enabled in replies.items()
    ):
        raise ValueError("reply_groups_invalid")
    if current and "ai_routes" in current:
        config.setdefault("ai_routes", copy.deepcopy(current["ai_routes"]))
    routes = config.get("ai_routes", {})
    if (
        not isinstance(routes, dict)
        or set(routes) - {"admin_provider_id", "groups"}
        or not isinstance(routes.get("admin_provider_id", ""), str)
        or len(routes.get("admin_provider_id", "")) > 256
        or not isinstance(routes.get("groups", {}), dict)
        or len(routes.get("groups", {})) > 100
        or any(
            not isinstance(group, str)
            or group not in groups
            or not isinstance(provider_id, str)
            or len(provider_id) > 256
            for group, provider_id in routes.get("groups", {}).items()
        )
    ):
        raise ValueError("ai_routes_invalid")
    config["ai_routes"] = {
        "admin_provider_id": routes.get("admin_provider_id", ""),
        "groups": dict(routes.get("groups", {})),
    }
    # Preserve opt-ins when an older dashboard omits newer policy fields.
    if current:
        for field in (
            "content_rules_since",
            "semantic",
            "auto_kick",
            "card_auto",
            "card_suffix",
        ):
            if field in current.get("moderation", {}):
                config.setdefault("moderation", {}).setdefault(
                    field, copy.deepcopy(current["moderation"][field])
                )
    validate_policy(config.get("moderation", {}))
    moderation = config.get("moderation", {})
    if moderation.get("enabled") and any(
        enabled
        and group in groups
        and "rename" not in moderation.get("permissions", {}).get(group, [])
        for group, enabled in moderation.get("card_auto", {}).items()
    ):
        raise ValueError("请启用机器人群管并授权本群修改普通成员群名片，再开启自动规范")
    draft = config.get("developer_test", {})
    if (
        not isinstance(draft, dict)
        or set(draft)
        - {"sender_instance", "groups", "scopes", "keywords", "seconds", "budget"}
        or not isinstance(draft.get("sender_instance", ""), str)
        or len(draft.get("sender_instance", "")) > 256
        or not isinstance(draft.get("groups", []), list)
        or any(
            not isinstance(g, str) or g not in groups for g in draft.get("groups", [])
        )
        or not isinstance(draft.get("scopes", []), list)
        or any(
            s
            not in (
                "private_commands",
                "private_ai",
                "group_commands",
                "group_ai",
                "group_rules",
            )
            for s in draft.get("scopes", [])
        )
        or not isinstance(draft.get("keywords", []), list)
        or len(draft.get("keywords", [])) > 10
        or any(
            not isinstance(w, str) or not w.startswith("WSL_TEST_") or len(w) > 100
            for w in draft.get("keywords", [])
        )
        or type(draft.get("seconds", 300)) is not int
        or not 1 <= draft.get("seconds", 300) <= 300
        or type(draft.get("budget", 10)) is not int
        or not 1 <= draft.get("budget", 10) <= 10
    ):
        raise ValueError("developer_test_invalid")
    proactive = config.get("proactive_send", {})
    if (
        not isinstance(proactive, dict)
        or type(proactive.get("enabled", False)) is not bool
    ):
        raise ValueError("proactive_send_invalid")
    targets = proactive.get("targets", [])
    if (
        not isinstance(targets, list)
        or len(targets) > 1000
        or any(
            not isinstance(target, str) or not target or len(target) > 2048
            for target in targets
        )
    ):
        raise ValueError("proactive_targets_invalid")
    config["proactive_send"] = {
        **proactive,
        "targets": list(dict.fromkeys(targets)),
    }
    if not isinstance(groups, list) or any(
        not isinstance(group, str)
        or not group.isascii()
        or not group.isdigit()
        or not 0 < int(group) < 1 << 30
        for group in groups
    ):
        raise ValueError("enabled_groups_invalid")
    config["enabled_groups"] = list(dict.fromkeys(groups))
    async with registrations.commit_lock:
        instance = config["id"]
        tx = None
        reference = config.get("session_ref")
        if reference:
            tx = registrations.claim(owner, instance, reference)
            if current and current.get("account_id") != tx.account_id:
                raise ValueError("account_change_requires_new_instance")
        elif current is None:
            raise ValueError("login_required")
        account = tx.account_id if tx else current["account_id"]
        if any(
            bot.get("type") == "wangshangliao"
            and bot.get("id") != instance
            and bot.get("account_id") == account
            for bot in service.config.get("platform", [])
        ):
            raise ValueError("account_already_configured")
        clean = {k: v for k, v in config.items() if k != "session_ref"}
        clean["account_id"] = account
        if tx:
            clean["nickname"] = tx.nickname
        if runtime.service and not runtime.snapshot(clean):
            runtime.service.load(clean)
        if runtime.snapshot(clean):
            for key in BUSINESS_FIELDS:
                clean.pop(key, None)
        if current and not tx and clean == current:
            if business_change is not None:
                await runtime.service.save(
                    {**current, **business_change}, expected, "dashboard:" + owner
                )
            return
        if business_change is not None:
            raise ValueError("请分别保存连接设置与插件业务设置")
        vault = Vault(instance)
        previous = vault.load()
        previous_platforms = list(service.config["platform"])
        if current:
            await service.core_lifecycle.platform_manager.terminate_platform(instance)
        try:
            if tx:
                registrations.claim(owner, instance, reference)
                vault.save(
                    {
                        **tx.saved,
                        "registration": {
                            "owner": owner,
                            "reference_hash": hashlib.sha256(
                                reference.encode()
                            ).hexdigest(),
                        },
                    }
                )
            if current:
                service.config["platform"][:] = [
                    clean if bot.get("id") == instance else bot
                    for bot in previous_platforms
                ]
            else:
                service.config["platform"].append(clean)
            persist(service.config, service.config, is_core=True)
        except BaseException:
            service.config["platform"][:] = previous_platforms
            if previous is not None:
                vault.save(previous)
            else:
                vault.clear()
            if current:
                await service.core_lifecycle.platform_manager.load_platform(current)
            raise
        if tx:
            code = next(
                code
                for code, value in registrations.transactions.items()
                if value is tx
            )
            await registrations.discard(code)
        await service.core_lifecycle.platform_manager.load_platform(clean)


async def handle_registration(platform_manager, payload: dict, owner: str = "") -> dict:
    """Handle owner-bound native login and management requests.

    Args:
        platform_manager: Existing platform manager, without replacing its lifecycle.
        payload: The original Dashboard registration request.
        owner: Authenticated Dashboard principal.

    Returns:
        The original native operation result.

    Raises:
        PlatformServiceError: On validation or native operation failure.
    """
    from astrbot.dashboard.services.platform_service import PlatformServiceError

    from .registration import (
        registrations,
    )
    from .storage import Vault
    from .wire import ProtocolError

    adapter = None
    try:
        if not owner:
            raise ProtocolError("dashboard_login_required")
        if payload.get("action") in {
            "card_preview",
            "cleanup_preview",
            "card_execute",
            "card_status",
            "card_stop",
        }:
            from astrbot.core.star import star_registry

            plugin = next(
                (
                    entry.star_cls
                    for entry in star_registry
                    if entry.name == "wangshangliao_moderation"
                    and entry.activated
                    and entry.star_cls is not None
                ),
                None,
            )
            adapter = next(
                (
                    p
                    for p in platform_manager.platform_insts
                    if p.meta().name == "wangshangliao"
                    and p.meta().id == payload.get("instance_id")
                ),
                None,
            )
            if plugin is None or adapter is None:
                raise ProtocolError("card_service_unavailable")
            binding = "dashboard/" + owner
            action = payload["action"]
            if action in {"card_preview", "cleanup_preview"}:
                return await plugin.cards.preview(
                    adapter,
                    str(payload.get("group", "")),
                    binding,
                    member=payload.get("card_member"),
                    card_name=payload.get("card_name"),
                    cleanup=action == "cleanup_preview",
                    cleanup_limit=payload.get("cleanup_limit"),
                    cleanup_state=payload.get("cleanup_state"),
                )
            job_id = str(payload.get("card_job_id", ""))
            if action == "card_execute":
                return plugin.cards.start(adapter, job_id, binding)
            if action == "card_stop":
                return plugin.cards.stop(adapter, job_id, binding)
            return await plugin.cards.refresh_status(adapter, job_id, binding)
        if payload.get("action") in {"test_status", "test_open", "test_close"}:
            from .runtime_policy import (
                runtime,
            )

            if payload.get("action") != "test_status" and not (
                runtime.service and runtime.service.active
            ):
                raise ProtocolError("business_service_inactive")
            from .test_window import (
                TestWindow,
            )

            instances = [
                p
                for p in platform_manager.platform_insts
                if p.meta().name == "wangshangliao"
            ]
            adapter = next(
                (p for p in instances if p.meta().id == payload.get("instance_id")),
                None,
            )
            if adapter is None:
                raise ProtocolError("instance_not_found")
            action = payload["action"]
            if action == "test_close":
                adapter.test_window = None
                logger.info("WSL developer window closed: bot=%s", adapter.meta().id)
            elif action == "test_open":
                sender = next(
                    (
                        p
                        for p in instances
                        if p.meta().id == payload.get("sender_instance")
                    ),
                    None,
                )
                if (
                    sender is None
                    or adapter.connection_state != "online"
                    or sender.connection_state != "online"
                ):
                    raise ProtocolError("test_instances_offline")
                if adapter.test_window and adapter.test_window.status()["active"]:
                    raise ProtocolError("test_window_already_active")
                adapter.test_window = TestWindow(
                    adapter,
                    sender,
                    payload.get("groups", []),
                    payload.get("scopes", []),
                    payload.get("keywords", []),
                    payload.get("seconds", 300),
                    payload.get("budget", 10),
                )
                sender.test_command_window = adapter.test_window
                logger.info(
                    "WSL developer window opened: bot=%s source=%s",
                    adapter.meta().id,
                    sender.meta().id,
                )
            return {
                "window": adapter.test_window.status()
                if adapter.test_window
                else {"active": False, "remaining": 0, "seconds": 0},
                "instances": [
                    {"id": p.meta().id, "account": p.account}
                    for p in instances
                    if p is not adapter
                ],
            }
        if payload.get("action") in {
            "moderation_preview",
            "moderation_execute",
        }:
            from .approval import (
                approval,
            )

            instance = payload.get("instance_id")
            config = next(
                (
                    c
                    for c in platform_manager.astrbot_config["platform"]
                    if c.get("id") == instance and c.get("type") == "wangshangliao"
                ),
                None,
            )
            if config is None:
                raise ProtocolError("instance_not_found")
            from .runtime_policy import (
                runtime,
            )

            config = runtime.view(config)
            saved = Vault(instance).load()
            if not saved or str(saved["business"]["uid"]) != str(
                config.get("account_id")
            ):
                raise ProtocolError("reauth_required")
            from .policy import (
                authorize_action,
            )

            if payload["action"] == "moderation_preview":
                authorize_action(
                    config,
                    str(payload.get("group")),
                    payload.get("operation_action"),
                )
            operation = await asyncio.to_thread(
                approval, instance, owner, saved, payload
            )
            if payload["action"] == "moderation_preview":
                return operation
            authorize_action(config, str(operation["group"]), operation["action"])
            adapter = next(
                (p for p in platform_manager.platform_insts if p.meta().id == instance),
                None,
            )
            if adapter is None or adapter.account != operation["account"]:
                raise ProtocolError("not_online")
            return await adapter.execute_moderation(
                **{k: v for k, v in operation.items() if k != "account"}
            )
        if payload.get("action") == "groups" and not payload.get("registration_code"):
            import aiohttp

            from .business import (
                BusinessClient,
                Deployment,
            )

            if set(payload) != {"action", "instance_id"}:
                raise ProtocolError("registration_input")
            instance = payload["instance_id"]
            config = next(
                (
                    c
                    for c in platform_manager.astrbot_config["platform"]
                    if c.get("id") == instance and c.get("type") == "wangshangliao"
                ),
                None,
            )
            if config is None:
                raise ProtocolError("instance_not_found")
            adapter = next(
                (p for p in platform_manager.platform_insts if p.meta().id == instance),
                None,
            )
            from .directory import (
                group_directory,
            )

            if (
                adapter
                and adapter.business
                and adapter.http
                and not adapter.http.closed
            ):
                return await group_directory(
                    adapter.business, str(config.get("account_id"))
                )
            saved = Vault(instance).load()
            if not saved or str(saved["business"]["uid"]) != str(
                config.get("account_id")
            ):
                raise ProtocolError("reauth_required")
            async with aiohttp.ClientSession() as http:
                client = BusinessClient(Deployment.load(), http)
                client.restore(saved["business"])
                return await group_directory(client, str(config.get("account_id")))
        if payload.get("action") == "logout":
            if set(payload) != {"action", "instance_id"}:
                raise ProtocolError("registration_input")
            instance = payload["instance_id"]
            configs = platform_manager.astrbot_config["platform"]
            config = next(
                (
                    c
                    for c in configs
                    if c.get("id") == instance and c.get("type") == "wangshangliao"
                ),
                None,
            )
            if config is None:
                raise ProtocolError("instance_not_found")
            await platform_manager.terminate_platform(instance)
            for code, tx in list(registrations.transactions.items()):
                if tx.instance == instance:
                    await registrations.discard(code)
            Vault(instance).clear()
            return {"status": "reauth_required"}
        result = await registrations.action(owner, payload)
        return result
    except ProtocolError as exc:
        if payload.get("action") in {
            "card_preview",
            "cleanup_preview",
            "card_execute",
            "card_status",
            "card_stop",
        }:
            from .diagnostics import Diagnostics

            Diagnostics(getattr(adapter, "config", {}).get("id", "unknown")).emit(
                "card_request",
                "rejected",
                str(payload.get("card_job_id", "")),
                group=str(payload.get("group", "")),
                failed=True,
                error=str(exc),
            )
        raise PlatformServiceError(str(exc), 400) from None
    except Exception:
        raise PlatformServiceError("registration_failed", 500) from None
