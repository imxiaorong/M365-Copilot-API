"""OpenAI Chat Completions response shaping — non-streaming path.

The streaming path lives inline in :mod:`server.api` because it needs to
thread state (chunk id, conversation id, error tuple) across many yields.
"""

from __future__ import annotations

import time
import uuid
from typing import Optional

from .config import MODEL_NAME


def _completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def build_completion(text: str, conversation_id: Optional[str] = None) -> dict:
    """Return one OpenAI-shape ChatCompletion for ``text``."""
    return {
        "id": _completion_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_NAME,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        # Copilot doesn't report token usage — expose the Copilot conversation
        # id so callers can resume the thread on the next request.
        "conversation_id": conversation_id,
    }
