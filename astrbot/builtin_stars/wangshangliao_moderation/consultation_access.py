"""Read-only consultation routing from transport-bound, account-scoped grants."""

import hashlib
import json
import time

from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity

from .open_store import OpenError


def consultation_access(service, event):
    """Snapshot guidance visibility without granting command or model authority.

    Args:
        service: Integrated service owning the committed group assignments.
        event: Private event with its independently authenticated transport sender.

    Returns:
        Minimal caller-owned role metadata, profile kind and a scope fingerprint.

    Raises:
        OpenError: If the event is not bound to this account and private sender.
    """
    identity = event.actor_identity
    account = str(event.platform.account)
    if (
        not event.is_private_chat()
        or not isinstance(identity, ActorIdentity)
        or identity.account != account
        or identity.claimed_uid != str(event.get_sender_id())
        or not identity.peer
    ):
        raise OpenError("private_identity_unverified")
    configured = any(
        row["kind"] == "admin_guidance" and not row["account"] and not row["group_id"]
        for row in service.profiles.rows()
    )
    access = {
        "routing_enabled": configured,
        "verified": identity.verified_uid == identity.claimed_uid,
        "management": [],
        "finance": [],
        "support_editor": None,
        "selected_group": "",
        "rules_only": False,
    }
    if configured and access["verified"]:
        support = getattr(service, "support", None)
        editor = support.editor(identity) if support else None
        if editor:
            access["support_editor"] = {
                "account": account,
                "version": editor["version"],
            }
        config = event.platform.config
        selected = service.plugin.commands.selections.get(
            (
                id(event.platform),
                account,
                identity.claimed_uid,
                event.unified_msg_origin,
            ),
            {},
        )
        if selected.get("expires", 0) > time.monotonic():
            access["selected_group"] = selected.get("group", "")
        for row in sorted(
            service.store.tenants(account), key=lambda row: row["group_id"]
        ):
            group = row["group_id"]
            if (
                row["account"] != account
                or identity.verified_uid not in row["managers"]
                or not row["active"]
                or not row["present"]
                or row.get("admission", "legacy") not in {"legacy", "verified"}
                or row.get("cleanup_epoch", 0) > row.get("cleanup_done", 0)
                or group not in config.get("enabled_groups", [])
                or group in service.retired_groups
            ):
                continue
            key = (account, group)
            member = service.member_evidence.get(key, {}).get(identity.verified_uid, {})
            native_verified = (
                str(member.get("nimId", "")) == identity.peer
                and member.get("groupRole") in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}
                and service.capabilities.get(key, {}).get("checked_at", 0)
                >= time.time() - 60
            )
            if not native_verified:
                continue
            access["management"].append(
                {
                    "group": group,
                    "version": row["version"],
                    "epoch": row["epoch"],
                    "native_verified": native_verified,
                    "ready": bool(
                        native_verified
                        and not row["paused"]
                        and row.get("native_role")
                        in {"GROUP_ROLE_ADMIN", "GROUP_ROLE_OWNER"}
                        and key not in service.suspended_groups
                        and not service.reconciling_groups.get(key)
                        and not service.membership_receiving.get(key)
                    ),
                }
            )
        cashier = getattr(getattr(service.plugin, "cashback", None), "service", None)
        store = getattr(cashier, "store", None)
        if callable(getattr(store, "finance_grants", None)):
            access["finance"] = sorted(
                [
                    {
                        "group": row["group_id"],
                        "role": row["role"],
                        "version": row["version"],
                    }
                    for row in store.finance_grants(
                        account, identity.verified_uid, identity.peer
                    )
                    if row["account"] == account
                    and row["uid"] == identity.verified_uid
                    and row["peer"] == identity.peer
                    and row["active"]
                    and row["role"] in {"cashier", "finance_admin"}
                ],
                key=lambda row: row["group"],
            )
        if access["selected_group"] not in {
            row["group"] for row in access["management"]
        }:
            access["selected_group"] = ""
    access["kind"] = (
        "admin_guidance"
        if access["management"] or access["finance"] or access["support_editor"]
        else "consultation"
    )
    owners = getattr(service, "owner_support", None)
    if owners:
        business = owners.route(event)
        access["business"] = business
        dedicated = owners.editor(identity) if access["verified"] else None
        if dedicated:
            access["dedicated_editor"] = {
                "uid": dedicated["uid"],
                "version": dedicated["version"],
            }
            if configured:
                access["kind"] = "admin_guidance"
        if business["explicit"]:
            access["kind"] = "consultation"
        if access["kind"] == "consultation":
            target = business["target"]
            access["support_scope"] = target["scope"] if target else account
            if target and target["rules_only"]:
                access["rules_only"] = True
    if access["verified"]:
        managed = [
            row
            for row in service.store.tenants(account)
            if row["active"]
            and row["present"]
            and identity.verified_uid in row["managers"]
            and row.get("admission", "legacy") in {"legacy", "verified"}
        ]
        selected = access["selected_group"]
        access["rules_only"] = access["rules_only"] or bool(
            any(
                row["group_id"] == selected and row.get("rules_only") for row in managed
            )
            or managed
            and all(row.get("rules_only") for row in managed)
            and not access["finance"]
            and not access["support_editor"]
        )
        if access["rules_only"] and access["kind"] == "admin_guidance":
            from .guidance_trial import trial_access

            grant = trial_access(
                service.store, account, identity, access["management"], selected
            )
            if grant:
                access["guidance_trial"] = grant
    access["fingerprint"] = hashlib.sha256(
        json.dumps([account, identity.principal, access], sort_keys=True).encode()
    ).hexdigest()
    return access
