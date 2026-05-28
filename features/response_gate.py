import time

DEFAULT_COOLDOWN_SECONDS = 10


class ResponseGate:
    """Shared cooldown gate.

    The original bot uses a single `last_response_time` to throttle both keyword
    auto-responses and random teases so the bot never spams the channel. The gate
    centralises that state so multiple features can share the same cooldown.
    """

    def __init__(self, cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS):
        self.cooldown = cooldown_seconds
        self._last_response = 0.0

    def can_respond(self) -> bool:
        return time.time() - self._last_response >= self.cooldown

    def mark_responded(self) -> None:
        self._last_response = time.time()
