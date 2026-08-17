"""OpenAI Responses API — request flattening and response shaping.

Responses API is a superset of Chat Completions with a very different wire
shape. This module contains everything specific to it: turning the polymorphic
``input`` field into one Copilot prompt, and emitting either a single
Response object (non-streaming) or the multi-event SSE stream that clients
like the new Codex CLI expect.

Reference for the event names and object shapes:
  https://platform.openai.com/docs/api-reference/responses
"""

from __future__ import annotations

import time
import uuid
from typing import Iterable, List, Optional, Union

from .config import MODEL_NAME
from .schemas import InputMessage, ResponsesRequest


# ------------------------------------------------------------------
# input → single prompt
# ------------------------------------------------------------------


def flatten_input(req: ResponsesRequest, has_conversation_id: bool) -> str:
    """Turn a Responses API request into one plain-text prompt for Copilot.

    Rules mirror the Chat Completions flattener in ``prompt.py``:
      * With ``conversation_id`` — trust the server-side memory; only the
        newest user text + any late system/instructions preamble is sent.
      * Without — synthesise the transcript as labelled lines so the model
        sees the earlier turns as context in a single-turn prompt.
    """
    turns = _collect_turns(req)

    # ``instructions`` is Responses API's system-prompt shorthand. Treat it as
    # a very-early system message.
    if req.instructions:
        turns.insert(0, ("system", req.instructions))

    if not turns:
        return ""

    # Find the last user turn.
    last_user_idx: Optional[int] = None
    for i in range(len(turns) - 1, -1, -1):
        if turns[i][0] == "user":
            last_user_idx = i
            break
    if last_user_idx is None:
        # No user turn — fall back to concatenating whatever text we have.
        return "\n\n".join(t for _, t in turns if t).strip()

    if has_conversation_id:
        preamble = [t for r, t in turns[:last_user_idx]
                    if r in ("system", "developer") and t]
        parts = preamble + [turns[last_user_idx][1]]
        return "\n\n".join(p for p in parts if p).strip()

    lines: List[str] = []
    for i, (role, text) in enumerate(turns):
        if not text:
            continue
        label = {
            "system": "System",
            "developer": "System",
            "user": "User",
            "assistant": "Assistant",
            "tool": "Tool",
        }.get(role, role.capitalize() if role else "User")
        if i == last_user_idx and role == "user":
            lines.append(text)
        else:
            lines.append(f"{label}: {text}")
    return "\n\n".join(lines).strip()


def _collect_turns(req: ResponsesRequest) -> List[tuple]:
    """Return an ordered list of (role, text) from the request's ``input``."""
    inp = req.input
    if inp is None:
        return []
    if isinstance(inp, str):
        return [("user", inp)]

    out: List[tuple] = []
    for item in inp:
        role = item.role or "user"
        text = _content_to_text(item)
        if text:
            out.append((role, text))
    return out


def _content_to_text(msg: InputMessage) -> str:
    """Extract text from a message's ``content`` (string or content-parts list)."""
    content = msg.content
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    # Content-parts list: pick every part with a `text` field. Ignores image /
    # file parts — Copilot's wire has no equivalent for those yet.
    return "\n".join(p.text for p in content if p and p.text)


# ------------------------------------------------------------------
# Non-streaming response
# ------------------------------------------------------------------


def _response_id() -> str:
    return f"resp_{uuid.uuid4().hex}"


def _output_item_id() -> str:
    return f"msg_{uuid.uuid4().hex}"


def build_response(
    text: str, conversation_id: Optional[str] = None, model: Optional[str] = None,
) -> dict:
    """Return a Responses-API ``Response`` object for a completed turn."""
    created = int(time.time())
    return {
        "id": _response_id(),
        "object": "response",
        "created_at": created,
        "status": "completed",
        "model": model or MODEL_NAME,
        "output": [{
            "id": _output_item_id(),
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{
                "type": "output_text",
                "text": text,
                "annotations": [],
            }],
        }],
        "usage": None,
        # Custom pass-through — Codex CLI ignores unknown fields, our own
        # clients can use it to keep multi-turn state.
        "conversation_id": conversation_id,
    }


# ------------------------------------------------------------------
# Streaming — SSE events
# ------------------------------------------------------------------
#
# The Responses API stream is a sequence of typed events. Each event is one
# SSE frame with an ``event:`` line and a JSON ``data:`` line. Codex CLI
# accepts several of these; the minimum set for a text-only turn is:
#
#   response.created                — envelope with the (empty) Response
#   response.in_progress            — optional heartbeat
#   response.output_item.added      — a new message item begins
#   response.content_part.added     — an output_text content part begins
#   response.output_text.delta      — one or more delta events with text
#   response.output_text.done       — the content part's final text
#   response.content_part.done      — content part closed
#   response.output_item.done       — item closed
#   response.completed              — envelope with the final Response
#
# We emit all of these because Codex CLI validates the item lifecycle.


def format_stream(
    text_chunks: Iterable[str],
    conversation_id_ref: List[Optional[str]],
    final_text_ref: List[str],
    model: Optional[str] = None,
) -> Iterable[str]:
    """Yield SSE-formatted Responses stream events.

    ``conversation_id_ref`` and ``final_text_ref`` are single-element lists the
    caller mutates when those values become available (mid-stream). This lets
    the closing ``response.completed`` event carry the definitive values even
    though the generator was constructed before we had them.
    """
    resp_id = _response_id()
    item_id = _output_item_id()
    created = int(time.time())
    mdl = model or MODEL_NAME
    sequence = 0

    def event(name: str, data: dict) -> str:
        nonlocal sequence
        data = {"type": name, "sequence_number": sequence, **data}
        sequence += 1
        return _sse_event(name, data)

    base_response = {
        "id": resp_id,
        "object": "response",
        "created_at": created,
        "status": "in_progress",
        "model": mdl,
        "output": [],
    }

    # 1. envelope
    yield event("response.created", {"response": {**base_response}})
    yield event("response.in_progress", {"response": {**base_response}})

    # 2. output item begins
    empty_item = {
        "id": item_id,
        "type": "message",
        "status": "in_progress",
        "role": "assistant",
        "content": [],
    }
    yield event("response.output_item.added", {
        "output_index": 0,
        "item": empty_item,
    })
    yield event("response.content_part.added", {
        "item_id": item_id,
        "output_index": 0,
        "content_index": 0,
        "part": {"type": "output_text", "text": "", "annotations": []},
    })

    # 3. deltas
    emitted = ""
    for chunk in text_chunks:
        if not chunk:
            continue
        emitted += chunk
        yield event("response.output_text.delta", {
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "delta": chunk,
        })

    # 4. content + item close
    final = final_text_ref[0] or emitted
    yield event("response.output_text.done", {
        "item_id": item_id,
        "output_index": 0,
        "content_index": 0,
        "text": final,
    })
    yield event("response.content_part.done", {
        "item_id": item_id,
        "output_index": 0,
        "content_index": 0,
        "part": {
            "type": "output_text",
            "text": final,
            "annotations": [],
        },
    })

    final_item = {
        "id": item_id,
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{
            "type": "output_text",
            "text": final,
            "annotations": [],
        }],
    }
    yield event("response.output_item.done", {
        "output_index": 0,
        "item": final_item,
    })

    # 5. envelope close
    final_response = {
        **base_response,
        "status": "completed",
        "output": [final_item],
        "usage": None,
        "conversation_id": conversation_id_ref[0],
    }
    yield event("response.completed", {"response": final_response})


def format_stream_error(status: int, kind: str, message: str) -> str:
    """One-off SSE event to surface an error mid-stream to Responses clients."""
    return _sse_event("response.failed", {
        "type": "response.failed",
        "sequence_number": 0,
        "response": {
            "status": "failed",
            "error": {"type": kind, "code": status, "message": message},
        },
    })


def _sse_event(name: str, data: dict) -> str:
    import json
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
