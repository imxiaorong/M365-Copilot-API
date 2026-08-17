"""Pydantic schemas for the OpenAI-compatible endpoints.

Only the fields we actually consume are typed. The server tolerates extra
fields silently — most OpenAI SDK requests carry things like ``user``,
``top_p``, ``presence_penalty`` that M365 Copilot doesn't honour but which we
shouldn't reject either.
"""

from __future__ import annotations

from typing import Any, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


# ------------------------------------------------------------------
# /v1/chat/completions  — Chat Completions API (legacy, still supported)
# ------------------------------------------------------------------


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"] = "user"
    content: str = ""


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: List[ChatMessage] = Field(default_factory=list)
    stream: bool = False
    # Custom pass-through: continue an existing Copilot conversation.
    # OpenAI doesn't have this concept — clients can either pass it explicitly
    # or use the `user` field as a stable identifier and let the server keep a
    # map. We take the explicit-param route because it's simpler and stateless.
    conversation_id: Optional[str] = None


# ------------------------------------------------------------------
# /v1/responses  — Responses API (what new Codex CLI insists on)
# ------------------------------------------------------------------
#
# Responses API replaces `messages` with a more polymorphic `input` field that
# can be:
#   * a plain string,
#   * a list of "input items", where each item is either
#       - a message (role/content pair, content itself may be string or a list
#         of typed content parts), or
#       - a function call, tool output, etc. (we ignore these — Copilot has no
#         native tool-use).
#
# We only need enough shape to flatten `input` back into a single prompt
# string. Everything else — reasoning params, response_format, tools, ...
# — is accepted and ignored, because Codex CLI will send them regardless
# and pydantic's default is to reject unknown fields under strict mode.


class InputContentPart(BaseModel):
    """One element inside a message's ``content`` array.

    Real Responses API types: input_text / output_text / input_image /
    input_file. We only extract text-bearing ones."""
    model_config = ConfigDict(extra="allow")

    type: Optional[str] = None
    text: Optional[str] = None


class InputMessage(BaseModel):
    """One message item inside ``input`` when it's a list."""
    model_config = ConfigDict(extra="allow")

    type: Optional[str] = None        # usually "message" or omitted
    role: Optional[str] = None        # "user"/"assistant"/"system"/"developer"
    # Content is a string OR a list of content parts, mirroring the wire spec.
    content: Union[str, List[InputContentPart], None] = None


class ResponsesRequest(BaseModel):
    """OpenAI Responses API request.

    We are lenient about extras (``model_config extra="allow"``) because Codex
    CLI ships a large set of fields per call (reasoning.effort,
    instructions, tools, store, previous_response_id, etc.) and only a few
    of them map to anything Copilot can honour.
    """
    model_config = ConfigDict(extra="allow")

    model: Optional[str] = None
    # ``input`` is either a bare string prompt or a list of typed items.
    input: Union[str, List[InputMessage], None] = None
    # Some clients also send system prompts / persona via ``instructions``.
    # We prepend it to the user text when present.
    instructions: Optional[str] = None
    stream: bool = False

    # Custom pass-through — same story as the Chat Completions endpoint.
    conversation_id: Optional[str] = None
