"""Native conversation manager with isolated, deterministic database methods."""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.core.conversation_mgr import ConversationManager


def conversation_manager():
    """Exercise the native manager without touching production SQLite or preferences."""
    rows = {}

    async def create(**fields):
        row = SimpleNamespace(
            **fields,
            conversation_id=str(uuid.uuid4()),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            token_usage=0,
        )
        row.content = row.content or []
        rows[row.conversation_id] = row
        return row

    async def update(cid, **fields):
        row = rows.get(cid)
        if row:
            for key, value in fields.items():
                if value is not None:
                    setattr(row, key, value)
            row.updated_at = datetime.now(timezone.utc)

    return ConversationManager(
        SimpleNamespace(
            rows=rows,
            create_conversation=AsyncMock(side_effect=create),
            get_conversation_by_id=AsyncMock(side_effect=lambda cid: rows.get(cid)),
            update_conversation=AsyncMock(side_effect=update),
            delete_conversation=AsyncMock(side_effect=lambda cid: rows.pop(cid, None)),
        )
    )
