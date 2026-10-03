"""Administrator-owned, expiring login attempts; never reuse an active session."""

import asyncio
import json
import re
import secrets
import time
from contextlib import suppress

from filelock import FileLock
from telethon import TelegramClient, errors
from telethon.sessions import MemorySession, SQLiteSession

from . import registry


class LoginError(ValueError):
    pass


class AccountLogin:
    def __init__(self):
        self.pending = {}
        self.lock = asyncio.Lock()
        self.next_start = 0

    async def discard(self):
        item, self.pending = self.pending, {}
        if item:
            item["timer"].cancel()
            if item.get("authorized") and not item.get("committed"):
                with suppress(Exception):
                    await asyncio.wait_for(item["client"].log_out(), 5)
            await item["client"].disconnect()

    async def expire(self, ticket):
        async with self.lock:
            if self.pending.get("ticket") == ticket:
                await self.discard()

    async def close(self):
        async with self.lock:
            await self.discard()

    async def start(self, owner, body):
        alias = body.get("alias", "")
        phone = body.get("phone", "")
        api_hash = body.get("api_hash", "")
        api_id = body.get("api_id")
        if (
            not isinstance(alias, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", alias)
            or not isinstance(phone, str)
            or not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone)
            or type(api_id) is not int
            or not 0 < api_id < 2**31
            or not isinstance(api_hash, str)
            or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash)
            or body.get("consent") is not True
        ):
            raise LoginError("请检查账号别名、国际区号手机号、API 凭据和账号授权。")
        async with self.lock:
            if self.pending or time.monotonic() < self.next_start:
                raise LoginError("已有登录申请或发送过于频繁，请取消原申请或稍后再试。")
            if alias in registry.load():
                raise LoginError("账号别名已存在。")
            self.next_start = time.monotonic() + 60
            client = TelegramClient(
                MemorySession(),
                api_id,
                api_hash,
                receive_updates=False,
                flood_sleep_threshold=0,
                request_retries=0,
                connection_retries=0,
                timeout=10,
            )
            try:
                async with asyncio.timeout(30):
                    await client.connect()
                    sent = await client.send_code_request(phone)
                ticket = secrets.token_urlsafe(32)
                timer = asyncio.get_running_loop().call_later(
                    300, lambda: asyncio.create_task(self.expire(ticket))
                )
                self.pending = {
                    "ticket": ticket,
                    "owner": owner,
                    "alias": alias,
                    "phone": phone,
                    "api_id": api_id,
                    "api_hash": api_hash,
                    "client": client,
                    "code_hash": sent.phone_code_hash,
                    "state": "code",
                    "attempts": 0,
                    "expires": time.monotonic() + 300,
                    "timer": timer,
                }
                return {"ticket": ticket, "state": "code", "expires_in": 300}
            except errors.FloodWaitError as exc:
                await client.disconnect()
                self.next_start = time.monotonic() + max(60, exc.seconds)
                raise LoginError("Telegram 限流，请稍后再试。") from None
            except Exception:
                await client.disconnect()
                raise LoginError(
                    "验证码发送失败，请检查 API 凭据、手机号和服务器网络。"
                ) from None
            except asyncio.CancelledError:
                await client.disconnect()
                raise

    async def confirm(self, owner, body):
        async with self.lock:
            item = self.pending
            ticket = body.get("ticket")
            if (
                not item
                or item["owner"] != owner
                or not isinstance(ticket, str)
                or not ticket.isascii()
                or not secrets.compare_digest(ticket, item["ticket"])
            ):
                raise LoginError("登录申请不存在或不属于当前管理员。")
            if time.monotonic() >= item["expires"]:
                await self.discard()
                raise LoginError("登录申请已过期。")
            if body.get("cancel") is True:
                await self.discard()
                return {"state": "cancelled"}
            item["attempts"] += 1
            if item["attempts"] > 5:
                await self.discard()
                raise LoginError("验证次数过多，登录申请已取消。")
            try:
                async with asyncio.timeout(30):
                    if item["state"] == "password":
                        password = body.get("password")
                        if (
                            not isinstance(password, str)
                            or not 1 <= len(password) <= 256
                        ):
                            raise LoginError("请输入两步验证密码。")
                        await item["client"].sign_in(password=password)
                    else:
                        code = body.get("code", "")
                        if not isinstance(code, str) or not re.fullmatch(
                            r"[0-9]{4,8}", code
                        ):
                            raise LoginError("请输入正确格式的验证码。")
                        await item["client"].sign_in(
                            phone=item["phone"],
                            code=code,
                            phone_code_hash=item["code_hash"],
                        )
                    me = await item["client"].get_me()
                item["authorized"] = True
                if not me or me.bot:
                    raise LoginError("只能添加已授权的用户账号。")
                self.persist(item, me.id)
                item["committed"] = True
                alias = item["alias"]
                await self.discard()
                return {"state": "registered", "account": alias}
            except errors.SessionPasswordNeededError:
                item["state"] = "password"
                return {"state": "password"}
            except (errors.PhoneCodeInvalidError, errors.PasswordHashInvalidError):
                raise LoginError("验证码或两步验证密码不正确。") from None
            except LoginError:
                raise
            except Exception:
                await self.discard()
                raise LoginError("授权未完成，请刷新账号列表核验后重新申请。") from None

    def persist(self, item, user_id):
        root = registry.private_root()
        with FileLock(str(root / "registry.lock"), timeout=0):
            records = registry.load()
            if item["alias"] in records or any(
                str(r["user_id"]) == str(user_id) for r in records.values()
            ):
                raise LoginError("该账号或别名已经登记，不能重复添加。")
            session = root / ("session_" + item["alias"])
            path = session.with_suffix(".session")
            # Exclusive creation avoids replacing recovery files or symlinks.
            with path.open("xb"):
                path.chmod(0o600)
            saved = None
            committed = False
            try:
                saved = SQLiteSession(str(session))
                source = item["client"].session
                saved.set_dc(source.dc_id, source.server_address, source.port)
                saved.auth_key = source.auth_key
                saved.save()
                saved.close()
                saved = None
                records[item["alias"]] = {
                    "user_id": user_id,
                    "session": str(session),
                    "api_id": item["api_id"],
                    "api_hash": item["api_hash"],
                }
                target = root / "accounts.json"
                temp = root / ("accounts-" + secrets.token_hex(12) + ".tmp")
                try:
                    with temp.open("x", encoding="utf-8") as file:
                        temp.chmod(0o600)
                        json.dump(records, file, indent=2)
                    temp.replace(target)
                    committed = True
                finally:
                    temp.unlink(missing_ok=True)
            finally:
                if saved:
                    saved.close()
                if not committed:
                    path.unlink(missing_ok=True)
