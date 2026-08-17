"""High-level M365 Copilot client — the recommended entry point.

One client, many conversations addressed by ``conversation_id``. Handles auth
refresh transparently and normalizes streaming vs. buffered output.

    from m365copilot import M365CopilotClient

    client = M365CopilotClient()
    reply = client.chat("Hello")
    print(reply.text, reply.conversation_id)

    # Continue the same thread by passing the id back:
    reply2 = client.chat("And in French?", reply.conversation_id)

    # Stream chunk-by-chunk:
    for chunk in client.stream("Tell me a joke"):
        print(chunk, end="", flush=True)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Generator, List, Optional

from .auth import AUTH_MAX_AGE, load_auth
from .driver import AuthExpired, ChathubDriver, ChathubError, TurnResult


@dataclass
class ChatReply:
    text: str
    conversation_id: Optional[str] = None
    request_id: Optional[str] = None
    suggested_responses: List[str] = field(default_factory=list)
    throttling: dict = field(default_factory=dict)


class ChatStream:
    """Iterable of reply chunks that exposes conversation metadata."""

    def __init__(self, generator: Generator, initial_conversation_id: Optional[str]):
        self._gen = generator
        self.conversation_id = initial_conversation_id
        self.result: Optional[TurnResult] = None

    def __iter__(self):
        try:
            while True:
                chunk = next(self._gen)
                yield chunk
        except StopIteration as stop:
            self.result = stop.value if isinstance(stop.value, TurnResult) else None
            if self.result and self.result.conversation_id:
                self.conversation_id = self.result.conversation_id


class M365CopilotClient:
    def __init__(self, max_age: int = AUTH_MAX_AGE):
        self._driver = ChathubDriver()
        self._max_age = max_age
        self._auth: Optional[dict] = None

    def stream(self, prompt: str, conversation_id: Optional[str] = None,
               tone: Optional[str] = None, **kwargs) -> ChatStream:
        if tone is not None:
            kwargs["tone"] = tone
        gen = self._stream_with_recovery(prompt, conversation_id, kwargs)
        return ChatStream(gen, conversation_id)

    def chat(self, prompt: str, conversation_id: Optional[str] = None,
             tone: Optional[str] = None, **kwargs) -> ChatReply:
        if tone is not None:
            kwargs["tone"] = tone
        s = self.stream(prompt, conversation_id=conversation_id, **kwargs)
        chunks: List[str] = []
        for c in s:
            if isinstance(c, str):
                chunks.append(c)
        result = s.result
        return ChatReply(
            text="".join(chunks) or (result.final_text if result else ""),
            conversation_id=s.conversation_id,
            request_id=result.request_id if result else None,
            suggested_responses=result.suggested_responses if result else [],
            throttling=result.throttling if result else {},
        )

    # -- internals ----------------------------------------------------------

    def _stream_with_recovery(self, prompt, conversation_id, kwargs):
        """Drive one turn; on AuthExpired, refresh auth once and retry."""
        for attempt in range(2):
            auth = self._fresh_auth(force=attempt > 0)
            try:
                gen = self._driver.send(
                    prompt=prompt,
                    access_token=auth["access_token"],
                    object_id=auth["object_id"],
                    tenant_id=auth["tenant_id"],
                    conversation_id=conversation_id,
                    **kwargs,
                )
                # Manually pump the generator so we can capture the return value.
                result = None
                try:
                    while True:
                        chunk = next(gen)
                        yield chunk
                except StopIteration as stop:
                    result = stop.value if isinstance(stop.value, TurnResult) else None
                return result  # noqa: B901  — generator return, delivered as StopIteration.value
            except AuthExpired:
                if attempt == 1:
                    raise
                # Force a token refresh and retry once.
                self._auth = None

    # Override on instances that must NOT trigger an interactive login
    # (e.g. the FastAPI server process, which has no stdin for Enter). The
    # default loader falls back to a visible browser sign-in when the profile
    # is not yet signed in; server contexts set this to ``load_auth`` with
    # ``auto_login=False``.
    _auth_loader = staticmethod(load_auth)

    def _fresh_auth(self, force: bool = False) -> dict:
        if force or self._auth is None or (
            time.time() - self._auth.get("saved_at", 0)
        ) >= self._max_age:
            self._auth = self._auth_loader(max_age=self._max_age)
        return self._auth
