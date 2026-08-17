"""M365 Copilot Sydney Chathub driver — pure WebSocket, no browser required.

Speaks the SignalR JSON hub protocol against
``wss://substrate.office.com/m365Copilot/Chathub``. This is the low-level
engine; most callers should use :class:`m365copilot.client.M365CopilotClient`.

Flow (per turn):

  1. ``send``   {"protocol":"json","version":1}      -- handshake
  2. ``recv``   {}                                    -- handshake ack
  3. ``send``   {"type":6}                            -- ping
  4. ``send``   {"type":4,"target":"chat",...}        -- invocation (user msg)
  5. ``recv``   {"type":1,"target":"update",...}*     -- streaming updates
  6. ``recv``   {"type":2,"invocationId":"0",item:…}  -- completion (has convId)
  7. ``recv``   {"type":3,"invocationId":"0"}         -- ack, close socket

Streaming updates come in two shapes (see captures/chat_frames.md):

  a. Full-replace ``messages[0].text`` — the entire concatenated answer so far.
  b. ``writeAtCursor`` deltas addressed by a JSONPath ``cursor``.

We diff the full-replace form to emit token-by-token increments. writeAtCursor
frames are safe to ignore because the very next full-replace covers the same
content — the web client uses them for micro-animation, not for correctness.
"""

from __future__ import annotations

import json
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, Iterable, List, Optional, Tuple

from websocket import (
    WebSocket,
    WebSocketApp,
    WebSocketConnectionClosedException,
    WebSocketException,
    create_connection,
)

from . import protocol as proto


class ChathubError(RuntimeError):
    """Server sent an error frame, or the socket died mid-turn."""


class AuthExpired(ChathubError):
    """The Sydney token was rejected. Refresh with :mod:`m365copilot.browser`."""


@dataclass
class TurnResult:
    """Everything the caller might want after a turn finishes."""
    conversation_id: Optional[str] = None
    request_id: Optional[str] = None
    final_text: str = ""
    messages: List[Dict[str, Any]] = field(default_factory=list)
    throttling: Dict[str, Any] = field(default_factory=dict)
    suggested_responses: List[str] = field(default_factory=list)


class ChathubDriver:
    """One-shot turn driver against the Sydney Chathub.

    A new WebSocket is opened per turn (mirrors the web client — every message
    gets a fresh Chathub session, though the ``ConversationId`` is reused to
    keep the server-side memory).
    """

    def __init__(self, connect_timeout: int = 30, idle_timeout: int = 120):
        self.connect_timeout = connect_timeout
        self.idle_timeout = idle_timeout

    # -- public -------------------------------------------------------------

    def send(
        self,
        prompt: str,
        access_token: str,
        object_id: str,
        tenant_id: str,
        conversation_id: Optional[str] = None,
        locale: str = "zh-cn",
        time_zone: str = "Asia/Shanghai",
        time_zone_offset: int = 8,
        tone: Optional[str] = None,
    ) -> Generator[Any, None, TurnResult]:
        """Send ``prompt``, yield text increments, return :class:`TurnResult`.

        The generator's ``return`` value is available via ``StopIteration.value``
        after iteration finishes, or callers can wrap this with the higher-level
        :class:`m365copilot.client.M365CopilotClient` which does that for them.
        """
        session_id = proto._hex32()
        url = proto.build_chathub_url(
            object_id=object_id,
            tenant_id=tenant_id,
            access_token=access_token,
            conversation_id=conversation_id,
            session_id=session_id,
        )
        invocation = proto.build_invocation(
            prompt=prompt,
            session_id=session_id,
            locale=locale,
            time_zone=time_zone,
            time_zone_offset=time_zone_offset,
            is_start_of_session=conversation_id is None,
            **({"tone": tone} if tone else {}),
        )

        try:
            ws = create_connection(
                url,
                timeout=self.connect_timeout,
                # Substrate cert is fine; leaving sslopt in case of proxied setups.
                sslopt={"cert_reqs": ssl.CERT_REQUIRED},
            )
        except WebSocketException as exc:
            # 401 on the upgrade means the token is bad or expired.
            msg = str(exc)
            if "401" in msg or "Unauthorized" in msg:
                raise AuthExpired(f"Sydney token rejected on WS upgrade: {msg}") from exc
            raise ChathubError(f"Failed to open Chathub WS: {msg}") from exc

        try:
            yield from self._drive_turn(ws, invocation)
        finally:
            try:
                ws.close()
            except Exception:
                pass

    # -- protocol -----------------------------------------------------------

    def _drive_turn(self, ws: WebSocket, invocation: Dict) -> Generator[Any, None, TurnResult]:
        """Handshake, send, then stream updates until completion."""
        # 1. handshake
        self._send_frame(ws, {"protocol": "json", "version": 1})
        # 2. handshake ack — server replies {} (may be empty until we ping)
        ack = self._recv_next(ws)
        if ack and "error" in ack:
            raise ChathubError(f"Handshake rejected: {ack['error']}")

        # 3. keep-alive ping (the web client sends one before the invocation)
        self._send_frame(ws, {"type": proto.MSG_PING})

        # 4. invocation
        self._send_frame(ws, invocation)

        # 5-7. drain until completion
        return (yield from self._stream_updates(ws, invocation.get("invocationId", "0")))

    def _stream_updates(
        self, ws: WebSocket, invocation_id: str
    ) -> Generator[Any, None, TurnResult]:
        result = TurnResult()
        emitted = ""

        while True:
            frame = self._recv_next(ws)
            if frame is None:
                # Socket closed cleanly. If we didn't see a completion, that's an error.
                if not result.final_text:
                    raise ChathubError("Chathub socket closed before completion")
                return result

            mtype = frame.get("type")

            if mtype == proto.MSG_INVOCATION:
                target = frame.get("target")
                if target != "update":
                    continue  # ignore Metrics echoes and other targets
                args = frame.get("arguments") or []
                if not args:
                    continue
                update = args[0]

                # Populate throttling and requestId early — they arrive in the
                # first update frame before any text.
                if "throttling" in update and not result.throttling:
                    result.throttling = update["throttling"]
                if "requestId" in update and not result.request_id:
                    result.request_id = update["requestId"]

                # Full-replace text lives in messages[0].text. We only diff the
                # first bot message (there may be trailing Progress/Suggestion
                # messages in the same array we don't want to emit).
                new_text = self._extract_bot_text(update)
                if new_text is not None and new_text != emitted:
                    delta = new_text[len(emitted):] if new_text.startswith(emitted) else new_text
                    if delta:
                        emitted = new_text
                        yield delta

                # Capture suggested follow-ups when they arrive.
                for m in update.get("messages") or []:
                    if m.get("messageType") == "Suggestion":
                        text = m.get("text") or m.get("commandText")
                        if text:
                            result.suggested_responses.append(text)
                    for s in m.get("suggestedResponses") or []:
                        text = s.get("text") or s.get("commandText")
                        if text and text not in result.suggested_responses:
                            result.suggested_responses.append(text)

            elif mtype == proto.MSG_COMPLETION:
                if frame.get("invocationId") != invocation_id:
                    continue
                item = frame.get("item") or {}
                if item.get("conversationId"):
                    result.conversation_id = item["conversationId"]
                if item.get("requestId") and not result.request_id:
                    result.request_id = item["requestId"]
                if item.get("throttling"):
                    result.throttling = item["throttling"]

                final = (item.get("result") or {}).get("message")
                if final:
                    # If completion carries a longer final than we streamed, emit the tail.
                    if final != emitted:
                        tail = final[len(emitted):] if final.startswith(emitted) else final
                        if tail:
                            yield tail
                            emitted = final
                    result.final_text = final
                    result.messages = item.get("messages") or []

                # Surface server-side errors ('result.value' != 'Success').
                value = (item.get("result") or {}).get("value")
                if value and value != "Success":
                    raise ChathubError(
                        f"Chathub returned result.value={value!r}: "
                        f"{(item.get('result') or {}).get('message')}"
                    )

            elif mtype == proto.MSG_COMPLETION_ACK:
                # Server done — no more frames coming.
                if not result.final_text:
                    result.final_text = emitted
                return result

            elif mtype == proto.MSG_PING:
                # Server keep-alive; ignore.
                continue

            elif "error" in frame:
                raise ChathubError(f"Chathub error frame: {frame['error']}")

    # -- primitives ---------------------------------------------------------

    @staticmethod
    def _extract_bot_text(update: Dict[str, Any]) -> Optional[str]:
        """Return the first bot message's text from an update frame, if any.

        The update carries either a full ``messages[]`` array (the common
        streaming shape) or a ``writeAtCursor`` delta. We only diff the former;
        the deltas are redundant for our purposes because the very next
        messages frame supersedes them.
        """
        for msg in update.get("messages") or []:
            if msg.get("author") != "bot":
                continue
            if msg.get("messageType") in ("Suggestion", "ReferencesListComplete"):
                continue
            text = msg.get("text")
            if isinstance(text, str):
                return text
        return None

    def _send_frame(self, ws: WebSocket, payload: Dict) -> None:
        """Send one SignalR JSON hub frame (JSON + record separator)."""
        try:
            ws.send(json.dumps(payload) + proto.RECORD_SEPARATOR)
        except WebSocketException as exc:
            raise ChathubError(f"WS send failed: {exc}") from exc

    def _recv_next(self, ws: WebSocket) -> Optional[Dict]:
        """Receive the next SignalR frame, or None if the socket closed cleanly.

        Buffers across packets: SignalR splits frames on 0x1E and one WS packet
        may carry several frames concatenated. We keep un-parsed remainder in
        an instance buffer so subsequent calls pick up mid-stream frames.
        """
        # Return any buffered frame from a previous packet first.
        pending = getattr(self, "_pending", [])
        if pending:
            self._pending = pending[1:]
            try:
                return json.loads(pending[0])
            except json.JSONDecodeError:
                return None

        ws.settimeout(self.idle_timeout)
        try:
            raw = ws.recv()
        except WebSocketConnectionClosedException:
            return None
        except WebSocketException as exc:
            raise ChathubError(f"WS recv failed: {exc}") from exc

        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not raw:
            return None

        # Split on record separator. Drop empty tail from trailing separator.
        parts = [p for p in raw.split(proto.RECORD_SEPARATOR) if p]
        if not parts:
            return None
        self._pending = parts[1:]
        try:
            return json.loads(parts[0])
        except json.JSONDecodeError:
            return None
