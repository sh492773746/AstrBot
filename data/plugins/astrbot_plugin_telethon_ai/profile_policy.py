"""Role-specific Dashboard schemas and server-side profile isolation checks."""

import copy

NAME = "astrbot_plugin_telethon_ai"


class ProfilePolicy:
    def __init__(self, manager, config, tenants):
        self.manager = manager
        self.config = config
        self.tenants = tenants

    def bindings(self, config_id):
        if config_id == "default":
            return []
        platforms = {p["id"]: p for p in self.manager.default_conf.get("platform", [])}
        customers = {
            row[0] for row in self.tenants.db.execute("SELECT platform FROM tenants")
        }
        control = self.config.get("control_platform_id", "VIP_DHBot")
        bindings = []
        for route, target in self.manager.ucr.umop_to_conf_id.items():
            if target != config_id:
                continue
            platform_id = route.split(":", 1)[0]
            platform = platforms.get(platform_id, {})
            role = None
            if platform_id == control:
                if self.config.get("controller_mode", "standalone") == "standalone":
                    role = "controller"
            elif platform_id in customers:
                role = "customer"
            elif platform.get("type") == "telethon_ai":
                role = "telethon"
            bindings.append((route, role))
        return bindings

    def role(self, config_id):
        bindings = self.bindings(config_id)
        roles = {role for _, role in bindings if role}
        if not roles:
            return None
        # A product profile must not be shared with another role or unrelated Bot.
        if len(roles) != 1 or any(role is None for _, role in bindings):
            return "conflict"
        return roles.pop()

    @staticmethod
    def editable_path(path):
        if path.startswith(
            (
                "agent_runner.config.model.",
                "agent_runner.config.persona.",
                "agent_runner.config.compression.",
                "content_safety.",
                "platform_settings.rate_limit.",
            )
        ):
            return True
        return path in {
            "provider_settings.enable",
            "provider_settings.identifier",
            "provider_settings.datetime_system_prompt",
            "provider_settings.prompt_prefix",
            "platform_settings.reply_prefix",
        }

    def describe(self, config_id, metadata):
        role = self.role(config_id)
        if role is None:
            return None
        filtered = {}
        if role == "telethon":
            for section, schema in metadata.items():
                if section == "plugin_group":
                    continue
                groups = {}
                for key, group in schema.get("metadata", {}).items():
                    items = {
                        path: item
                        for path, item in group.get("items", {}).items()
                        if self.editable_path(path)
                    }
                    if items:
                        groups[key] = {
                            **copy.deepcopy(group),
                            "items": copy.deepcopy(items),
                        }
                if groups:
                    filtered[section] = {**schema, "metadata": groups}
        return {
            "role": role,
            "title": {
                "controller": "总控 Bot",
                "customer": "克隆管理 Bot",
                "telethon": "Telethon AI 账号",
                "conflict": "配置档路由冲突",
            }[role],
            "editable": role == "telethon",
            "runner_locked": True,
            "routes": [route for route, _ in self.bindings(config_id)],
            "plugin": NAME,
            "metadata": filtered,
        }

    @staticmethod
    def changed_paths(before, after, prefix=""):
        if isinstance(before, dict) and isinstance(after, dict):
            for key in before.keys() | after.keys():
                path = f"{prefix}.{key}" if prefix else key
                if key not in before or key not in after:
                    yield path
                else:
                    yield from ProfilePolicy.changed_paths(
                        before[key], after[key], path
                    )
        elif before != after or type(before) is not type(after):
            yield prefix

    def validate(self, config_id, incoming):
        role = self.role(config_id)
        if role is None:
            return
        if role == "conflict":
            raise ValueError("配置档被不同角色共用，请先修正路由绑定。")
        current = self.manager.confs[config_id]
        changed = list(self.changed_paths(current, incoming))
        denied = [
            path
            for path in changed
            if role != "telethon" or not self.editable_path(path)
        ]
        if denied:
            raise ValueError(
                "此配置档的隔离或业务设置不可修改：" + "、".join(sorted(denied)[:8])
            )
        if role == "telethon":
            if (
                incoming.get("admins_id") != []
                or incoming.get("disable_builtin_commands") is not True
                or incoming.get("plugin_set") != [NAME]
                or incoming.get("kb_names") != []
                or incoming.get("agent_runner", {}).get("runner_type") != "local"
            ):
                raise ValueError("Telethon AI 配置档的隔离状态异常，请联系平台运维。")

    def validate_delete(self, config_id):
        if self.role(config_id) is not None:
            raise ValueError("配置档仍绑定账号或服务 Bot，请先由平台运维解除绑定。")
