"""Shared asyncio lock that serialises upstream Sydney calls.

Both request handlers (api.py) and the keep-alive refresher (keepalive.py)
need to serialise access to the single Sydney account. Putting the lock in
its own module avoids the circular dependency that would arise if either
imported from the other.
"""

from __future__ import annotations

import asyncio
from typing import Optional

_lock: Optional[asyncio.Lock] = None


def get_upstream_lock() -> asyncio.Lock:
    """Return the singleton upstream lock, creating it lazily on first call.

    Lazy creation is important: ``asyncio.Lock()`` binds to the current event
    loop, and creating it at module-import time (before the loop runs) would
    capture a different loop than the one handling requests.
    """
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock