"""Bound dedicated bot ingress without generating extra flood responses."""

from collections import OrderedDict, deque
from time import monotonic


class BotIngressLimit:
    """Apply bounded per-user and global sliding windows."""

    def __init__(self):
        self.users = OrderedDict()
        self.total = deque()
        self.rejected = 0

    def allow(self, user_id, now=None):
        """Reserve capacity before expensive processing.

        Args:
            user_id: Telegram sender identity.
            now: Optional monotonic timestamp for deterministic tests.

        Returns:
            Whether this request fits both rate budgets.
        """
        now = monotonic() if now is None else now
        while self.total and self.total[0] <= now - 60:
            self.total.popleft()
        events = self.users.get(user_id, deque())
        while events and events[0] <= now - 60:
            events.popleft()
        if (
            len(self.total) >= 120
            or len(events) >= 20
            or sum(t > now - 5 for t in events) >= 5
        ):
            self.rejected += 1
            return False
        if user_id not in self.users and len(self.users) >= 2048:
            self.users.popitem(last=False)
        self.users[user_id] = events
        self.users.move_to_end(user_id)
        events.append(now)
        self.total.append(now)
        return True
