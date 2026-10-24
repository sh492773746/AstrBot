"""Reject stale imports of the retired natural-language management service."""

import json


class AdminConfigDrafts:
    """Preserve a fail-closed import without configuration or execution capability."""

    def __init__(self, context, schedules, activities):
        """Accept the old constructor without retaining any service authority.

        Args:
            context: Ignored legacy context.
            schedules: Ignored legacy schedule service.
            activities: Ignored legacy activity service.
        """
        self.drafts = {}

    async def preview(self, event, value):
        """Reject every old proposal without reading its event or payload.

        Args:
            event: Ignored legacy event.
            value: Ignored legacy proposal.

        Returns:
            The fixed retirement result.
        """
        return json.dumps({"status": "rejected", "reason": "ai_management_retired"})

    async def confirm(self, event, value):
        """Reject every old confirmation without reading its event or payload.

        Args:
            event: Ignored legacy event.
            value: Ignored legacy confirmation.

        Returns:
            The fixed retirement result.
        """
        return json.dumps({"status": "rejected", "reason": "ai_management_retired"})
