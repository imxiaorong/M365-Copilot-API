"""Token-bucket rate limiter.

Adapted from Windows-Copilot-API/server/ratelimit.py — same shape, different
tuning defaults (M365 Copilot's throttling.metering block gives a more
generous ceiling than consumer Copilot's ~15 rpm).
"""

from __future__ import annotations

import threading
import time
from typing import Optional


class TokenBucket:
    """Classic token-bucket: ``capacity`` burst, refilled at ``rpm`` per minute.

    ``rpm=0`` disables the limiter entirely.
    """

    def __init__(self, rpm: int, capacity: int):
        self.rpm = max(0, rpm)
        self.capacity = max(1, capacity)
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def take(self) -> Optional[float]:
        """Try to consume one token. Returns None on success, or the seconds
        the caller should wait before retrying (Retry-After)."""
        if self.rpm == 0:
            return None
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.updated
            self.updated = now
            # Refill at rpm/60 tokens per second, cap at capacity.
            self.tokens = min(self.capacity, self.tokens + elapsed * (self.rpm / 60.0))
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return None
            deficit = 1.0 - self.tokens
            wait = deficit / (self.rpm / 60.0)
            return round(wait, 3)
