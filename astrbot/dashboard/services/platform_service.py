from __future__ import annotations

import secrets
import string

from astrbot.core import logger
from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
from astrbot.core.platform import Platform
from astrbot.core.platform.sources.dingtalk.app_registration import (
    poll_dingtalk_app_registration_once,
    request_dingtalk_app_registration,
)
from astrbot.core.platform.sources.lark.app_registration import (
    poll_app_registration_once,
    request_app_registration,
)
from astrbot.core.platform.sources.lark.bot_info import request_lark_bot_info
from astrbot.core.platform.sources.qqofficial.login_registration import (
    poll_qqofficial_login_once,
    request_qqofficial_login_qr,
)
from astrbot.core.platform.sources.weixin_oc.login_registration import (
    poll_weixin_oc_login_once,
    request_weixin_oc_login_qr,
)


class PlatformServiceError(Exception):
    def __init__(self, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code


def random_platform_id_suffix() -> str:
    return "_" + "".join(secrets.choice(string.ascii_lowercase) for _ in range(4))


class PlatformService:
    def __init__(self, core_lifecycle: AstrBotCoreLifecycle) -> None:
        self.platform_manager = core_lifecycle.platform_manager

    async def handle_webhook_callback(self, webhook_uuid: str, request_obj):
        platform_adapter = self.find_platform_by_uuid(webhook_uuid)

        if not platform_adapter:
            logger.warning(f"未找到 webhook_uuid 为 {webhook_uuid} 的平台")
            raise PlatformServiceError("未找到对应平台", 404)

        try:
            return await platform_adapter.webhook_callback(request_obj)
        except NotImplementedError as exc:
            logger.error(
                f"平台 {platform_adapter.meta().name} 未实现 webhook_callback 方法"
            )
            raise PlatformServiceError("平台未支持统一 Webhook 模式", 500) from exc
        except Exception as exc:
            logger.error(f"处理 webhook 回调时发生错误: {exc}", exc_info=True)
            raise PlatformServiceError("处理回调失败", 500) from exc

    def find_platform_by_uuid(self, webhook_uuid: str) -> Platform | None:
        for platform in self.platform_manager.platform_insts:
            if platform.config.get("webhook_uuid") == webhook_uuid:
                if platform.unified_webhook():
                    return platform
        return None

    def get_platform_stats(self):
        try:
            return self.platform_manager.get_all_stats()
        except Exception as exc:
            logger.error(f"获取平台统计信息失败: {exc}", exc_info=True)
            raise PlatformServiceError(f"获取统计信息失败: {exc}", 500) from exc

    async def handle_platform_registration(
        self,
        platform_type: str,
        payload: dict,
        owner: str = "",
    ) -> dict:
        if platform_type == "wangshangliao":
            from astrbot.core.platform.sources.wangshangliao.registration import (
                registrations,
            )
            from astrbot.core.platform.sources.wangshangliao.storage import Vault
            from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

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
                            for p in self.platform_manager.platform_insts
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
                    from astrbot.core.platform.sources.wangshangliao.test_window import (
                        TestWindow,
                    )

                    instances = [
                        p
                        for p in self.platform_manager.platform_insts
                        if p.meta().name == "wangshangliao"
                    ]
                    adapter = next(
                        (
                            p
                            for p in instances
                            if p.meta().id == payload.get("instance_id")
                        ),
                        None,
                    )
                    if adapter is None:
                        raise ProtocolError("instance_not_found")
                    action = payload["action"]
                    if action == "test_close":
                        adapter.test_window = None
                        logger.info(
                            "WSL developer window closed: bot=%s", adapter.meta().id
                        )
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
                        if (
                            adapter.test_window
                            and adapter.test_window.status()["active"]
                        ):
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
                    from astrbot.core.platform.sources.wangshangliao.approval import (
                        approval,
                    )

                    instance = payload.get("instance_id")
                    config = next(
                        (
                            c
                            for c in self.platform_manager.astrbot_config["platform"]
                            if c.get("id") == instance
                            and c.get("type") == "wangshangliao"
                        ),
                        None,
                    )
                    if config is None:
                        raise ProtocolError("instance_not_found")
                    saved = Vault(instance).load()
                    if not saved or str(saved["business"]["uid"]) != str(
                        config.get("account_id")
                    ):
                        raise ProtocolError("reauth_required")
                    from astrbot.core.platform.sources.wangshangliao.policy import (
                        authorize_action,
                    )

                    if payload["action"] == "moderation_preview":
                        authorize_action(
                            config,
                            str(payload.get("group")),
                            payload.get("operation_action"),
                        )
                    operation = approval(instance, owner, saved, payload)
                    if payload["action"] == "moderation_preview":
                        return operation
                    authorize_action(
                        config, str(operation["group"]), operation["action"]
                    )
                    adapter = next(
                        (
                            p
                            for p in self.platform_manager.platform_insts
                            if p.meta().id == instance
                        ),
                        None,
                    )
                    if adapter is None or adapter.account != operation["account"]:
                        raise ProtocolError("not_online")
                    return await adapter.execute_moderation(
                        **{k: v for k, v in operation.items() if k != "account"}
                    )
                if payload.get("action") == "groups" and not payload.get(
                    "registration_code"
                ):
                    import aiohttp

                    from astrbot.core.platform.sources.wangshangliao.business import (
                        BusinessClient,
                        Deployment,
                    )

                    if set(payload) != {"action", "instance_id"}:
                        raise ProtocolError("registration_input")
                    instance = payload["instance_id"]
                    config = next(
                        (
                            c
                            for c in self.platform_manager.astrbot_config["platform"]
                            if c.get("id") == instance
                            and c.get("type") == "wangshangliao"
                        ),
                        None,
                    )
                    if config is None:
                        raise ProtocolError("instance_not_found")
                    adapter = next(
                        (
                            p
                            for p in self.platform_manager.platform_insts
                            if p.meta().id == instance
                        ),
                        None,
                    )
                    from astrbot.core.platform.sources.wangshangliao.directory import (
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
                        return await group_directory(
                            client, str(config.get("account_id"))
                        )
                if payload.get("action") == "logout":
                    if set(payload) != {"action", "instance_id"}:
                        raise ProtocolError("registration_input")
                    instance = payload["instance_id"]
                    configs = self.platform_manager.astrbot_config["platform"]
                    config = next(
                        (
                            c
                            for c in configs
                            if c.get("id") == instance
                            and c.get("type") == "wangshangliao"
                        ),
                        None,
                    )
                    if config is None:
                        raise ProtocolError("instance_not_found")
                    await self.platform_manager.terminate_platform(instance)
                    for code, tx in list(registrations.transactions.items()):
                        if tx.instance == instance:
                            await registrations.discard(code)
                    Vault(instance).clear()
                    return {"status": "reauth_required"}
                result = await registrations.action(owner, payload)
                if payload.get("action") == "start":
                    instance = payload["instance_id"]
                    if any(
                        config.get("id") == instance
                        and config.get("type") == "wangshangliao"
                        for config in self.platform_manager.astrbot_config["platform"]
                    ):
                        await self.platform_manager.terminate_platform(instance)
                return result
            except ProtocolError as exc:
                raise PlatformServiceError(str(exc), 400) from None
            except Exception:
                raise PlatformServiceError("registration_failed", 500) from None
        try:
            action = str(payload.get("action", "")).strip().lower()
            if not action:
                raise PlatformServiceError("Missing action", 400)

            platform_config = payload.get("platform_config")
            if not isinstance(platform_config, dict):
                platform_config = {}

            if platform_type == "lark":
                return await self._handle_lark_registration(
                    action,
                    payload,
                    platform_config,
                )
            if platform_type == "weixin_oc":
                return await self._handle_weixin_oc_registration(
                    action,
                    payload,
                    platform_config,
                )
            if platform_type == "dingtalk":
                return await self._handle_dingtalk_registration(action, payload)
            if platform_type in {"qq_official", "qq_official_webhook"}:
                return await self._handle_qqofficial_registration(
                    action,
                    payload,
                    platform_config,
                )

            raise PlatformServiceError(
                f"Unsupported platform registration: {platform_type}",
                404,
            )
        except PlatformServiceError:
            raise
        except Exception as exc:
            logger.error(f"处理平台一键创建请求失败: {exc}", exc_info=True)
            raise PlatformServiceError(str(exc), 500) from exc

    async def _handle_lark_registration(
        self,
        action: str,
        payload: dict,
        platform_config: dict,
    ) -> dict:
        domain = str(platform_config.get("domain") or "").strip()

        if action == "start":
            registration = await request_app_registration(domain)
            return {
                "status": "pending",
                "device_code": registration.device_code,
                "registration_code": registration.device_code,
                "user_code": registration.user_code,
                "verification_uri": registration.verification_uri,
                "verification_uri_complete": registration.verification_uri_complete,
                "expires_in": registration.expires_in,
                "interval": registration.interval,
            }

        if action == "poll":
            device_code = str(
                payload.get("device_code") or payload.get("registration_code") or ""
            ).strip()
            if not device_code:
                raise PlatformServiceError("Missing device_code", 400)
            result = await poll_app_registration_once(
                domain=domain,
                device_code=device_code,
            )
            if result.get("status") == "created":
                try:
                    bot_info = await request_lark_bot_info(
                        domain=str(result.get("domain") or domain),
                        app_id=str(result.get("app_id") or ""),
                        app_secret=str(result.get("app_secret") or ""),
                    )
                    if bot_info.app_name:
                        result["bot_name"] = bot_info.app_name
                    if bot_info.open_id:
                        result["bot_open_id"] = bot_info.open_id
                except Exception as exc:
                    logger.error(f"获取飞书机器人信息失败: {exc}", exc_info=True)
            return result

        raise PlatformServiceError(f"Unsupported action: {action}", 400)

    async def _handle_dingtalk_registration(
        self,
        action: str,
        payload: dict,
    ) -> dict:
        if action == "start":
            registration = await request_dingtalk_app_registration()
            return {
                "status": "pending",
                "device_code": registration.device_code,
                "registration_code": registration.device_code,
                "user_code": registration.user_code,
                "verification_uri": registration.verification_uri,
                "verification_uri_complete": registration.verification_uri_complete,
                "expires_in": registration.expires_in,
                "interval": registration.interval,
            }

        if action == "poll":
            device_code = str(
                payload.get("device_code") or payload.get("registration_code") or ""
            ).strip()
            if not device_code:
                raise PlatformServiceError("Missing device_code", 400)
            result = await poll_dingtalk_app_registration_once(device_code)
            if result.get("status") == "created":
                result["platform_id_suffix"] = random_platform_id_suffix()
            return result

        raise PlatformServiceError(f"Unsupported action: {action}", 400)

    async def _handle_qqofficial_registration(
        self,
        action: str,
        payload: dict,
        platform_config: dict,
    ) -> dict:
        if action == "start":
            registration = await request_qqofficial_login_qr(platform_config)
            return {
                "status": "pending",
                "registration_code": registration.task_id,
                "task_id": registration.task_id,
                "bind_key": registration.bind_key,
                "qrcode": registration.qrcode,
                "qrcode_img_content": registration.qrcode,
                "interval": registration.interval,
            }

        if action == "poll":
            task_id = str(
                payload.get("task_id") or payload.get("registration_code") or ""
            ).strip()
            bind_key = str(payload.get("bind_key") or "").strip()
            if not task_id:
                raise PlatformServiceError("Missing task_id", 400)
            if not bind_key:
                raise PlatformServiceError("Missing bind_key", 400)
            return await poll_qqofficial_login_once(
                platform_config=platform_config,
                task_id=task_id,
                bind_key=bind_key,
            )

        raise PlatformServiceError(f"Unsupported action: {action}", 400)

    async def _handle_weixin_oc_registration(
        self,
        action: str,
        payload: dict,
        platform_config: dict,
    ) -> dict:
        if action == "start":
            registration = await request_weixin_oc_login_qr(platform_config)
            return {
                "status": "pending",
                "registration_code": registration.qrcode,
                "qrcode": registration.qrcode,
                "qrcode_img_content": registration.qrcode_img_content,
                "interval": registration.interval,
            }

        if action == "poll":
            qrcode = str(
                payload.get("qrcode") or payload.get("registration_code") or ""
            ).strip()
            if not qrcode:
                raise PlatformServiceError("Missing qrcode", 400)
            result = await poll_weixin_oc_login_once(
                platform_config=platform_config,
                qrcode=qrcode,
            )
            if result.get("status") == "created":
                result["platform_id_suffix"] = random_platform_id_suffix()
            return result

        raise PlatformServiceError(f"Unsupported action: {action}", 400)
