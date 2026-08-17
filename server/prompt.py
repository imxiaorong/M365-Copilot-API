"""Turn an OpenAI ``messages`` array into a single Copilot prompt.

M365 Copilot's Chathub only accepts one user text field per turn. It has no
system/assistant channels — the whole conversation is a linear thread on the
server side, addressed by ``conversation_id``. Two consequences:

  * System messages become an inline preamble on the same turn.
  * Prior assistant turns are dropped when a ``conversation_id`` is supplied
    (the server already remembers them). Without a conversation_id we flatten
    the full transcript into a labelled block so context is at least visible
    in the single-turn prompt.
"""

from __future__ import annotations

from typing import List

from .schemas import ChatMessage


def flatten_messages(messages: List[ChatMessage], has_conversation_id: bool) -> str:
    """Return the single ``text`` payload to send to Copilot.

    With a conversation_id, only the newest user turn (plus any system messages
    fresher than it) is included — the rest is on the server. Without one, we
    emit the whole transcript as a plain-text block.
    """
    if not messages:
        return ""

    # Find the last user message; anything after it is a stray assistant echo we ignore.
    last_user_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "user":
            last_user_idx = i
            break
    if last_user_idx is None:
        # No user turn — fall back to concatenating whatever's there.
        return "\n\n".join(m.content for m in messages if m.content).strip()

    if has_conversation_id:
        # Server holds prior context; only include system messages newer than
        # the last user turn plus the user turn itself.
        preamble = [m.content for m in messages[:last_user_idx]
                    if m.role == "system" and m.content]
        parts = preamble + [messages[last_user_idx].content]
        return "\n\n".join(p for p in parts if p).strip()

    # No conversation context on the server — synthesise a transcript.
    lines = []
    for i, m in enumerate(messages):
        if not m.content:
            continue
        label = {
            "system": "System",
            "user": "User",
            "assistant": "Assistant",
            "tool": "Tool",
        }.get(m.role, m.role.capitalize())
        # Last user message goes bare — it's the actual question.
        if i == last_user_idx and m.role == "user":
            lines.append(m.content)
        else:
            lines.append(f"{label}: {m.content}")
    return "\n\n".join(lines).strip()
