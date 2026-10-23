"""Bounded AI work and conversation leases held through history persistence."""

import asyncio


class AIRuntime:
    """Own per-robot limits without spawning waiting tasks for an entire inbox."""

    def __init__(self):
        self.customers = {}
        self.sessions = {}

    async def customer(self, event):
        """Acquire a conversation lease before AstrBot loads its history.

        Args:
            event: Native event with an end-to-end completion future.

        Returns:
            Whether the event still has an active lease and business authority.
        """
        completion = getattr(event, "processing_completion", None)
        if not isinstance(completion, asyncio.Future):
            return True
        bot = (event.platform.config["id"], event.platform.account)
        key = (bot, event.unified_msg_origin)
        entry = self.sessions.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        semaphore = self.customers.setdefault(bot, asyncio.Semaphore(2))
        locked = admitted = released = False

        def release(_=None):
            nonlocal released
            if released:
                return
            released = True
            if admitted:
                semaphore.release()
            if locked:
                entry[0].release()
            entry[1] -= 1
            if entry[1] == 0:
                self.sessions.pop(key, None)

        try:
            await entry[0].acquire()
            locked = True
            await semaphore.acquire()
            admitted = True
            config = event.platform.config
            if (
                completion.done()
                or config.get("_business_active") is False
                or event.policy_epoch != config.get("_business_epoch")
            ):
                release()
                return False
            completion.add_done_callback(release)
            return True
        except BaseException:
            release()
            raise
