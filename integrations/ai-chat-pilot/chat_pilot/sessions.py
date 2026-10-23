"""Verify retired collector sessions without starting message collection."""

import json
from contextlib import ExitStack
from pathlib import Path


async def probe_sessions(config_path: Path) -> list[tuple[str, int]]:
    """Connect two existing accounts without reading dialogs or messages.

    Args:
        config_path: Retired collector's local private configuration.

    Returns:
        Non-secret account labels and Telegram user IDs.

    Raises:
        ValueError: Credentials, locks, or authorization are unavailable.
    """
    from filelock import FileLock, Timeout
    from telethon import TelegramClient

    try:
        config = json.loads(config_path.read_text())
        data = Path(config.get("data_dir", "var")).expanduser()
        if not data.is_absolute():
            data = config_path.parent / data
        specs = [
            (
                "collector",
                data / "account",
                config["api_id"],
                config["api_hash"],
                config.get("proxy"),
            )
        ]
        for item in config["unified"]["accounts"]:
            specs.append(
                (
                    item["id"],
                    Path(item["session"]).expanduser(),
                    item["api_id"],
                    item["api_hash"],
                    config.get("proxy"),
                )
            )
        if len(specs) != 2 or len({label for label, *_ in specs}) != 2:
            raise ValueError("Expected exactly two distinct accounts")
        if any(not Path(f"{session}.session").is_file() for _, session, *_ in specs):
            raise ValueError("A session file is missing")
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Collector configuration is unavailable") from error

    try:
        with ExitStack() as locks:
            locks.enter_context(FileLock(str(data / "collector.lock"), timeout=0))
            for _, session, *_ in specs:
                locks.enter_context(
                    FileLock(f"{session}.session.unified.lock", timeout=0)
                )
            identities = []
            for label, session, api_id, api_hash, proxy in specs:
                client = TelegramClient(
                    str(session),
                    int(api_id),
                    api_hash,
                    proxy=proxy,
                    receive_updates=False,
                    flood_sleep_threshold=0,
                    connection_retries=1,
                    request_retries=1,
                )
                try:
                    await client.connect()
                    if not await client.is_user_authorized():
                        raise ValueError(f"{label}: session is not authorized")
                    me = await client.get_me()
                    if me is None or me.bot:
                        raise ValueError(f"{label}: session is not a user account")
                    identities.append((label, me.id))
                finally:
                    await client.disconnect()
            if len({user_id for _, user_id in identities}) != len(identities):
                raise ValueError("Both sessions belong to the same account")
            return identities
    except Timeout as error:
        raise ValueError("A session is still held by another service") from error
