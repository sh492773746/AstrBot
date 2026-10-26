"""Explicit, bounded live acceptance scope; unknown sends are never repeated."""

import hashlib
import json
import secrets
import time

from .storage import SessionError, SessionStore, atomic_json


class AcceptanceWindow:
    def __init__(self, store: SessionStore):
        self.store = store

    def update(self, ref: str, owner: str, body: dict) -> dict:
        row = self.store.get(ref, owner)
        if row["state"] != "bound":
            raise SessionError("session_binding_required")
        path = self.store.directory(ref) / "acceptance.json"
        window = json.loads(path.read_text()) if path.is_file() else {}
        action = body.get("action", "status")
        if action == "open":
            if (
                body.get("confirm") is not True
                or body.get("account_id") != row["account_id"]
            ):
                raise SessionError("explicit_test_target_required")
            targets = body.get("targets")
            if (
                not isinstance(targets, list)
                or not 1 <= len(targets) <= 4
                or any(
                    not isinstance(item, dict)
                    or item.get("kind") not in {"private", "group"}
                    or not isinstance(item.get("target_id"), str)
                    or not 1 <= len(item["target_id"]) <= 128
                    or item["target_id"] != item["target_id"].strip()
                    for item in targets
                )
            ):
                raise ValueError("invalid_test_targets")
            duration, budget = body.get("duration", 300), body.get("budget", 10)
            if (
                type(duration) is not int
                or not 1 <= duration <= 300
                or type(budget) is not int
                or not 1 <= budget <= 10
            ):
                raise ValueError("invalid_test_budget")
            if window.get("expires_at", 0) > time.time() and not window.get(
                "closed", False
            ):
                raise SessionError("test_window_already_open")
            window = {
                "window_ref": secrets.token_hex(16),
                "account_id": row["account_id"],
                "expires_at": time.time() + duration,
                "budget": budget,
                "targets": [
                    {"kind": item["kind"], "target_id": item["target_id"]}
                    for item in targets
                ],
                "operations": {},
                "closed": False,
            }
            atomic_json(path, window)
        elif action == "close":
            window["closed"] = True
            atomic_json(path, window)
        elif action != "status":
            raise ValueError("invalid_test_action")
        return {
            "open": not window.get("closed", True)
            and window.get("expires_at", 0) > time.time(),
            "window_ref": window.get("window_ref", ""),
            "expires_at": window.get("expires_at", 0),
            "targets": window.get("targets", []),
            "remaining": max(
                0, window.get("budget", 0) - len(window.get("operations", {}))
            ),
            "receipts": window.get("operations", {}),
        }

    def reserve(self, ref: str, owner: str, body: dict) -> tuple[dict, bool]:
        row = self.store.get(ref, owner)
        path = self.store.directory(ref) / "acceptance.json"
        if not path.is_file():
            raise SessionError("test_window_closed")
        window = json.loads(path.read_text())
        if body.get("window_ref") != window.get("window_ref"):
            raise SessionError("test_window_mismatch")
        operation = body.get("operation_id")
        if (
            not isinstance(operation, str)
            or not 1 <= len(operation) <= 64
            or not all(c.isascii() and (c.isalnum() or c in "-_") for c in operation)
        ):
            raise ValueError("invalid_operation_id")
        image = body.get("image_base64")
        text = body.get("text", "")
        if (
            not isinstance(text, str)
            or len(text.encode()) > 16384
            or (not text and image is None)
        ):
            raise ValueError("invalid_test_text")
        if image is not None:
            if (
                not isinstance(image, str)
                or len(image) > 2 << 20
                or body.get("reply_to")
            ):
                raise ValueError("invalid_test_image")
            import base64

            from .media import validate_bytes

            try:
                validate_bytes(base64.b64decode(image, validate=True))
            except Exception:
                raise ValueError("invalid_test_image") from None
        target = {"kind": body.get("kind"), "target_id": body.get("target_id")}
        reply_to = body.get("reply_to", "")
        if (
            not isinstance(reply_to, str)
            or len(reply_to) > 128
            or reply_to != reply_to.strip()
        ):
            raise ValueError("invalid_quote_id")
        envelope = {**target, "text": text}
        if image is not None:
            envelope["image_digest"] = hashlib.sha256(image.encode()).hexdigest()
        if reply_to:
            envelope["reply_to"] = reply_to
        digest = hashlib.sha256(
            json.dumps(envelope, sort_keys=True).encode()
        ).hexdigest()
        previous = window.get("operations", {}).get(operation)
        if previous:
            if previous["request_digest"] != digest:
                raise SessionError("operation_id_conflict")
            return previous["result"], False
        if (
            window.get("closed")
            or window["expires_at"] <= time.time()
            or window.get("account_id") != row["account_id"]
        ):
            raise SessionError("test_window_closed")
        if target not in window["targets"]:
            raise SessionError("test_target_denied")
        if len(window["operations"]) >= window["budget"]:
            raise SessionError("test_budget_exhausted")
        result = {"status": "unknown", "operation_id": operation}
        window["operations"][operation] = {"request_digest": digest, "result": result}
        atomic_json(path, window)
        return result, True

    def receipt(self, ref: str, operation: str, result: dict):
        path = self.store.directory(ref) / "acceptance.json"
        window = json.loads(path.read_text())
        window["operations"][operation]["result"] = result
        atomic_json(path, window)


class NativeTestPermit:
    """Local, short-lived consent for exact actions against an owned test bot.

    No Dashboard or model endpoint can create this permit. Native roles and
    administrator protection remain mandatory, including during acceptance.
    """

    def __init__(self, store: SessionStore):
        self.store = store

    @staticmethod
    def digest(body: dict) -> str:
        return hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def create(self, ref, owner, target_ref, group, operations, *, confirm=False):
        actor = self.store.get(ref, owner)
        target = self.store.get(target_ref, owner)
        if (
            confirm is not True
            or actor["state"] != "bound"
            or target["state"] != "bound"
            or actor["account_id"] == target["account_id"]
            or not isinstance(group, str)
            or not 1 <= len(group) <= 128
            or not isinstance(operations, list)
            or not 1 <= len(operations) <= 10
        ):
            raise SessionError("explicit_test_target_required")
        allowed = {}
        for body in operations:
            operation = body.get("operation_id")
            if (
                body.get("action")
                not in {
                    "mute",
                    "unmute",
                    "kick",
                    "announce",
                    "revoke_member_message",
                    "set_member_group_nickname",
                }
                or body.get("session_ref") != ref
                or body.get("instance_id") != actor["instance_id"]
                or body.get("user_id")
                != (None if body.get("action") == "announce" else target["account_id"])
                or body.get("kind") != "group"
                or body.get("target_id") != group
                or body.get("confirm") is not True
                or not isinstance(operation, str)
                or not 8 <= len(operation) <= 128
                or operation in allowed
            ):
                raise SessionError("explicit_test_target_required")
            allowed[operation] = self.digest(body)
        now = time.time()
        path = self.store.directory(ref) / "native-test-permit.json"
        if path.exists():
            raise SessionError("test_permit_already_present")
        permit = {
            "owner": owner,
            "actor_ref": ref,
            "actor_account": actor["account_id"],
            "actor_instance": actor["instance_id"],
            "target_ref": target_ref,
            "target_account": target["account_id"],
            "target_instance": target["instance_id"],
            "group_id": group,
            "created_at": now,
            "expires_at": now + 300,
            "operations": allowed,
        }
        atomic_json(path, permit)

    def allows(self, ref, group, body):
        try:
            path = self.store.directory(ref) / "native-test-permit.json"
            if path.is_symlink() or path.stat().st_mode & 0o077:
                return False
            permit = json.loads(path.read_text())
            now = time.time()
            if not (
                permit["created_at"]
                <= now
                < permit["expires_at"]
                <= permit["created_at"] + 300
            ):
                return False
            actor = self.store.get(ref, permit["owner"])
            target = self.store.get(permit["target_ref"], permit["owner"])
            return (
                actor["state"] == target["state"] == "bound"
                and actor["account_id"] != target["account_id"]
                and ref == permit["actor_ref"] == body.get("session_ref")
                and actor["account_id"] == permit["actor_account"]
                and actor["instance_id"]
                == permit["actor_instance"]
                == body.get("instance_id")
                and target["account_id"]
                == permit["target_account"]
                == (
                    target["account_id"]
                    if body.get("action") == "announce"
                    else body.get("user_id")
                )
                and target["instance_id"] == permit["target_instance"]
                and group == permit["group_id"] == body.get("target_id")
                and body.get("kind") == "group"
                and body.get("action")
                in {
                    "mute",
                    "unmute",
                    "kick",
                    "announce",
                    "revoke_member_message",
                    "set_member_group_nickname",
                }
                and (body.get("action") != "announce" or body.get("user_id") is None)
                and permit["operations"].get(body.get("operation_id"))
                == self.digest(body)
            )
        except (OSError, ValueError, KeyError, TypeError, SessionError):
            return False

    def close(self, ref):
        (self.store.directory(ref) / "native-test-permit.json").unlink(missing_ok=True)


class GroupManagerTestPermit:
    """Local-only five-minute lease for delegation acceptance between owned bots.

    It does not grant authority itself, bypass native roles, change event.role,
    or lift ranking/enrollment/penalty protections. No web or model API creates it.
    """

    def __init__(self, store):
        self.store = store

    def create(self, ref, owner, target_ref, group, *, confirm=False):
        actor, target = self.store.get(ref, owner), self.store.get(target_ref, owner)
        if (
            confirm is not True
            or actor["state"] != "bound"
            or target["state"] != "bound"
            or actor["account_id"] == target["account_id"]
            or not isinstance(group, str)
            or not 1 <= len(group) <= 128
            or group != group.strip()
        ):
            raise SessionError("explicit_test_target_required")
        path = self.store.directory(ref) / "group-manager-test-permit.json"
        if path.exists():
            raise SessionError("test_permit_already_present")
        now = time.time()
        atomic_json(
            path,
            {
                "owner": owner,
                "actor_ref": ref,
                "actor_account": actor["account_id"],
                "actor_instance": actor["instance_id"],
                "target_ref": target_ref,
                "target_account": target["account_id"],
                "target_instance": target["instance_id"],
                "group_id": group,
                "created_at": now,
                "expires_at": now + 300,
            },
        )

    def allows(self, ref, group, uid):
        try:
            path = self.store.directory(ref) / "group-manager-test-permit.json"
            if path.is_symlink() or path.stat().st_mode & 0o077:
                return False
            permit = json.loads(path.read_text())
            now = time.time()
            if (
                not permit["created_at"]
                <= now
                < permit["expires_at"]
                <= permit["created_at"] + 300
            ):
                return False
            actor = self.store.get(ref, permit["owner"])
            target = self.store.get(permit["target_ref"], permit["owner"])
            return (
                actor["state"] == target["state"] == "bound"
                and actor["account_id"] != target["account_id"]
                and ref == permit["actor_ref"]
                and actor["account_id"] == permit["actor_account"]
                and actor["instance_id"] == permit["actor_instance"]
                and target["account_id"] == permit["target_account"] == str(uid)
                and target["instance_id"] == permit["target_instance"]
                and group == permit["group_id"]
            )
        except (OSError, ValueError, KeyError, TypeError, SessionError):
            return False

    def close(self, ref):
        (self.store.directory(ref) / "group-manager-test-permit.json").unlink(
            missing_ok=True
        )
