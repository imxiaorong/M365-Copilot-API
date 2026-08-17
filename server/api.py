"""FastAPI OpenAI-compatible bridge to M365 Copilot Chathub.

Concurrency: one Sydney account can't cleanly service parallel conversations,
so we serialise upstream calls behind an asyncio lock. Parallel HTTP requests
queue and run one at a time — same design as the consumer Copilot bridge.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

from m365copilot import M365CopilotClient
from m365copilot.auth import load_auth
from m365copilot.driver import AuthExpired, ChathubError

from .config import MODEL_NAME, RATE_LIMIT_BURST, RATE_LIMIT_RPM
from .keepalive import keepalive_loop
from .lock import get_upstream_lock
from .openai_format import build_completion
from .prompt import flatten_messages
from .ratelimit import TokenBucket
from .responses_format import flatten_input, build_response
from .schemas import ChatCompletionRequest, ResponsesRequest


# Root logger so Codex's request bodies and our error tracebacks both land
# somewhere visible. uvicorn's default config already formats these, we just
# need to bump the level so WARNING/INFO from the route handlers shows up.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)


app = FastAPI(title="M365 Copilot API", version="0.1.0")


@app.middleware("http")
async def catch_all_exceptions(request: Request, call_next):
    """Surface 500s with a real JSON body instead of plain-text 'Internal Server Error'.

    Codex CLI reports any non-2xx with `Unknown error` unless the body carries
    a parseable OpenAI-style error envelope. This middleware wraps any
    uncaught exception into a 502 with `{"error": {"message": str(exc), ...}}`
    so Codex at least shows useful diagnostics.
    """
    try:
        return await call_next(request)
    except Exception as exc:  # noqa: BLE001 — intentional last-resort catch
        body = {"error": {
            "message": f"{type(exc).__name__}: {exc}",
            "type": "internal_error",
        }}
        # Don't log the full traceback here — uvicorn's error logger already
        # did. Just make the response client-readable.
        return JSONResponse(status_code=502, content=body)


# Server-mode client: never pop a browser from inside a request handler. If
# there's no cached token, callers get a clean 401 and can run
# ``python -m m365copilot login`` on the host. Interactive login mid-request
# is a bad interaction model (invisible browser, half-broken state on Ctrl-C,
# and here the server has no stdin to press Enter into anyway).
def _server_auth_loader(**kw):
    return load_auth(auto_login=False, **kw)


_client = M365CopilotClient()
_client._auth_loader = _server_auth_loader  # override the default
_rate_limiter = TokenBucket(rpm=RATE_LIMIT_RPM, capacity=RATE_LIMIT_BURST)

# Sydney can't cleanly serve parallel conversations from one account, so we
# serialise every upstream call. The lock is lazy-constructed so it binds to
# whichever event loop is running when the first request lands — creating it
# at import time on Python 3.10+ works, but pre-3.10 asyncio.Lock() captures
# the current loop eagerly and later requests (on a different loop, e.g.
# uvicorn's) blow up with "attached to a different loop". Lazy fixes both.
# Shared upstream lock — lives in lock.py now so keepalive.py can also use
# it without creating a circular import. The lock serialises all calls to
# the single Sydney account; parallel requests queue behind it.


def _get_upstream_lock() -> asyncio.Lock:
    global _upstream_lock
    if _upstream_lock is None:
        _upstream_lock = asyncio.Lock()
    return _upstream_lock


_keepalive_task: Optional[asyncio.Task] = None


@app.on_event("startup")
async def _start_keepalive() -> None:
    """Kick off the background Sydney-token refresher.

    We deliberately don't await anything on boot — the token is either fresh
    from a recent `copilot login`, or the first request will trip through the
    normal `load_auth` path. Either way, the keep-alive task is only there to
    prevent the token from going stale during long idle periods.
    """
    global _keepalive_task
    _keepalive_task = asyncio.create_task(keepalive_loop())


@app.on_event("shutdown")
async def _stop_keepalive() -> None:
    if _keepalive_task and not _keepalive_task.done():
        _keepalive_task.cancel()
        try:
            await _keepalive_task
        except asyncio.CancelledError:
            pass

# Deadline sentinel for the streaming pump — kept for the Chat Completions
# path which still uses the old buffer-until-done pattern.
_STREAM_END = object()


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


# Codex CLI 0.145.0 probes this endpoint to verify provider reachability.
# If missing (404) it marks the provider as unreachable and refuses to send
# chat requests.
@app.get("/v1/health")
async def v1_health() -> dict:
    return {"status": "ok"}


# OpenAI ``model`` field → Copilot ``tone``. Callers pick which backend model
# they want by naming a different model in the request. Anything unknown
# falls back to the default Thinker tone.
MODEL_TONE_MAP = {
    "m365-copilot": "Gpt_5_6_Reasoning",           # default → Thinker
    "m365-copilot-thinker": "Gpt_5_6_Reasoning",
    "m365-copilot-fast": "Magic",                   # smart-routed non-thinker
    "m365-copilot-magic": "Magic",
    "m365-copilot-creative": "Creative",
    "m365-copilot-precise": "Precise",
    "m365-copilot-balanced": "Balanced",
    # Aliases so clients that use OpenAI's own model names still land on the
    # right Copilot tone without requiring the client to switch model names
    # when they switch providers. "gpt-5.6-sol" is what Codex CLI's TUI
    # shows, so we accept it and route to Thinker — Sol's closest analogue
    # on the Copilot backend.
    "gpt-5.6-sol": "Gpt_5_6_Reasoning",
    "gpt-5.6-thinker": "Gpt_5_6_Reasoning",
    "gpt-5.6-reasoning": "Gpt_5_6_Reasoning",
    "gpt-5.6": "Gpt_5_6_Reasoning",
}


def _tone_for_model(name: Optional[str]) -> str:
    """Return the Copilot tone for the given model name.

    All incoming requests map to the Deep Thinker tone, regardless of the
    ``model`` field the client sends.
    """
    return SYDNEY_TONE


# The canonical model name advertised to OpenAI-compatible clients. Set to
# "gpt-5.6-sol" so that Codex CLI's config model name (which is also
# "gpt-5.6-sol") matches exactly what the server returns in /v1/models and
# in every response payload. Codex 0.145.0 appears to validate that these
# match; if they don't it silently refuses to send chat requests.
CANONICAL_MODEL_NAME = "gpt-5.6-sol"


# The Copilot tone the Sydney backend will use. The name "Gpt_5_6_Reasoning"
# is Microsoft's internal tone identifier, copied verbatim from a real web
# capture (see captures/chat_frames.md). Microsoft doesn't publish a spec;
# what we know is that this is the *current* deepest-reasoning tone the
# Copilot Chat backend offers. New frontier models ship as new tone strings,
# so re-capture when one appears.
SYDNEY_TONE = "Gpt_5_6_Reasoning"


@app.get("/v1/models")
async def list_models() -> dict:
    # Advertise one canonical model name. Clients get exactly one choice
    # regardless of what model name they request — every request lands on
    # the Copilot Deep Thinker tone.
    return {
        "object": "list",
        "data": [{
            "id": CANONICAL_MODEL_NAME,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "microsoft",
        }, {
            "id": "Copilot-GPT-5.6-Reasoning",
            "object": "model",
            "created": int(time.time()),
            "owned_by": "microsoft",
        }],
    }


# ============================================================================
# /v1/chat/completions — legacy Chat Completions endpoint
# ============================================================================


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    wait = _rate_limiter.take()
    if wait is not None:
        raise HTTPException(
            status_code=429,
            detail={"error": {"message": "rate limit exceeded", "type": "rate_limit"}},
            headers={"Retry-After": str(int(wait) + 1)},
        )

    prompt = flatten_messages(req.messages, has_conversation_id=bool(req.conversation_id))
    if not prompt:
        raise HTTPException(status_code=400, detail={"error": {
            "message": "at least one non-empty user message is required",
            "type": "invalid_request_error",
        }})

    tone = _tone_for_model(req.model)

    if req.stream:
        return StreamingResponse(
            _stream_response(prompt, req.conversation_id, tone),
            media_type="text/event-stream",
        )
    return await _non_stream_response(prompt, req.conversation_id, tone)


# -- non-streaming ---------------------------------------------------------


async def _non_stream_response(
    prompt: str, conversation_id: Optional[str], tone: str,
) -> JSONResponse:
    async with get_upstream_lock():
        try:
            reply = await asyncio.to_thread(
                _client.chat, prompt, conversation_id, tone,
            )
        except AuthExpired as exc:
            raise HTTPException(status_code=401, detail={"error": {
                "message": str(exc), "type": "auth_expired",
            }})
        except ChathubError as exc:
            raise HTTPException(status_code=502, detail={"error": {
                "message": str(exc), "type": "upstream_error",
            }})
        except RuntimeError as exc:
            # e.g. "Not signed in" from load_auth. Surface as 401 so clients
            # know to re-authenticate rather than treating it as an outage.
            raise HTTPException(status_code=401, detail={"error": {
                "message": str(exc), "type": "not_signed_in",
            }})
    return JSONResponse(build_completion(reply.text, reply.conversation_id))


# -- streaming --------------------------------------------------------------


async def _stream_response(
    prompt: str, conversation_id: Optional[str], tone: str,
) -> AsyncGenerator[str, None]:
    """SSE stream of ChatCompletionChunk objects, [DONE] terminated.

    Bridges the synchronous ``M365CopilotClient.stream`` generator into async:
    a worker thread pumps chunks into an asyncio.Queue that this coroutine
    drains and formats as SSE.
    """
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    conv_id_ref = [conversation_id]

    async with get_upstream_lock():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def pump() -> None:
            try:
                stream = _client.stream(
                    prompt, conversation_id=conversation_id, tone=tone,
                )
                for chunk in stream:
                    if isinstance(chunk, str) and chunk:
                        loop.call_soon_threadsafe(queue.put_nowait, chunk)
                if stream.conversation_id:
                    conv_id_ref[0] = stream.conversation_id
            except AuthExpired as exc:
                loop.call_soon_threadsafe(
                    queue.put_nowait, ("error", 401, "auth_expired", str(exc)))
            except ChathubError as exc:
                loop.call_soon_threadsafe(
                    queue.put_nowait, ("error", 502, "upstream_error", str(exc)))
            except RuntimeError as exc:
                loop.call_soon_threadsafe(
                    queue.put_nowait, ("error", 401, "not_signed_in", str(exc)))
            except Exception as exc:  # noqa: BLE001 — surface any crash to the caller
                loop.call_soon_threadsafe(
                    queue.put_nowait, ("error", 500, "internal_error", str(exc)))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _STREAM_END)

        pump_task = loop.run_in_executor(None, pump)

        try:
            yield _sse(_role_chunk(cid, created))
            while True:
                item = await queue.get()
                if item is _STREAM_END:
                    break
                if isinstance(item, tuple) and item and item[0] == "error":
                    _, status, kind, message = item
                    yield _sse({"error": {"status": status, "type": kind, "message": message}})
                    return
                yield _sse(_content_chunk(cid, created, item))
            yield _sse(_final_chunk(cid, created, conv_id_ref[0]))
            yield "data: [DONE]\n\n"
        finally:
            await pump_task


# -- SSE chunk shaping ------------------------------------------------------


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _role_chunk(cid: str, created: int) -> dict:
    return {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": MODEL_NAME,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
    }


def _content_chunk(cid: str, created: int, content: str) -> dict:
    return {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": MODEL_NAME,
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }


def _final_chunk(cid: str, created: int, conversation_id: Optional[str]) -> dict:
    return {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": MODEL_NAME,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "conversation_id": conversation_id,
    }


# ============================================================================
# /v1/responses — Responses API (what Codex CLI now requires)
# ============================================================================
#
# The Responses API is a strict-schema successor to Chat Completions with a
# different wire shape (typed `input`, typed `output`, richer streaming
# events). Codex CLI v0.144+ enforces ``wire_api = "responses"``, so we speak
# it directly. Internally we reuse the same Copilot client — this endpoint is
# a shape adapter, not a second driver.


@app.post("/v1/responses")
async def responses(req: ResponsesRequest):
    # Codex 0.145.0 sends a "reachability probe" POST with no meaningful
    # payload body (empty input, no model). If we reject it with 400, Codex
    # marks the entire provider as unreachable (displayed as "502" in TUI).
    # Return a minimal 200 for every probe — the real validation happens on
    # actual chat requests which have a real input.
    prompt = flatten_input(req, has_conversation_id=bool(req.conversation_id))
    if not prompt or not prompt.strip():
        # Probe request — return a minimal but valid response.
        return JSONResponse(build_response("", req.conversation_id, req.model))

    # Log the request shape so we can debug Codex-side failures. Codex CLI
    # sends a richer payload than our pydantic schema models. Extra fields
    # are accepted (extra="allow") but the path through them might fail.
    logging.getLogger("m365.api").warning(
        "Responses request: model=%s stream=%s input_type=%s conv=%s keys=%s",
        req.model, req.stream,
        type(req.input).__name__,
        req.conversation_id,
        list(req.model_extra.keys()) if req.model_extra else [],
    )
    wait = _rate_limiter.take()
    if wait is not None:
        raise HTTPException(
            status_code=429,
            detail={"error": {"message": "rate limit exceeded", "type": "rate_limit"}},
            headers={"Retry-After": str(int(wait) + 1)},
        )

    prompt = flatten_input(req, has_conversation_id=bool(req.conversation_id))
    if not prompt:
        raise HTTPException(status_code=400, detail={"error": {
            "message": "input is required and must contain at least one text turn",
            "type": "invalid_request_error",
        }})

    tone = _tone_for_model(req.model)
    model_name = req.model or MODEL_NAME

    if req.stream:
        return StreamingResponse(
            _responses_stream(prompt, req.conversation_id, tone, model_name),
            media_type="text/event-stream",
        )
    return await _responses_non_stream(prompt, req.conversation_id, tone, model_name)


async def _responses_non_stream(
    prompt: str,
    conversation_id: Optional[str],
    tone: str,
    model_name: str,
) -> JSONResponse:
    async with get_upstream_lock():
        try:
            reply = await asyncio.to_thread(
                _client.chat, prompt, conversation_id, tone,
            )
        except AuthExpired as exc:
            raise HTTPException(status_code=401, detail={"error": {
                "message": str(exc), "type": "auth_expired",
            }})
        except ChathubError as exc:
            raise HTTPException(status_code=502, detail={"error": {
                "message": str(exc), "type": "upstream_error",
            }})
        except RuntimeError as exc:
            raise HTTPException(status_code=401, detail={"error": {
                "message": str(exc), "type": "not_signed_in",
            }})
    return JSONResponse(build_response(reply.text, reply.conversation_id, model_name))


async def _responses_stream(
    prompt: str,
    conversation_id: Optional[str],
    tone: str,
    model_name: str,
) -> AsyncGenerator[str, None]:
    """SSE stream of Responses API events for one turn.

    Each SSE event is yielded as soon as the corresponding Copilot output
    chunk arrives — no buffering. This is critical because Codex CLI's HTTP
    client will 502 if it sees no data for >~30 seconds.
    """
    resp_id = f"resp_{uuid.uuid4().hex}"
    created = int(time.time())
    conv_id_ref: list = [conversation_id]
    result_final_text: str = ""

    async with get_upstream_lock():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def pump() -> None:
            nonlocal result_final_text
            emitted = ""
            try:
                stream = _client.stream(
                    prompt, conversation_id=conversation_id, tone=tone,
                )
                for chunk in stream:
                    if isinstance(chunk, str) and chunk:
                        emitted += chunk
                        loop.call_soon_threadsafe(queue.put_nowait, ("delta", chunk))
                if stream.conversation_id:
                    conv_id_ref[0] = stream.conversation_id
                result = getattr(stream, "result", None)
                result_final_text = (
                    (result.final_text if result and result.final_text else emitted)
                )
            except AuthExpired as exc:
                loop.call_soon_threadsafe(queue.put_nowait, ("error", "auth_expired", str(exc)))
            except ChathubError as exc:
                loop.call_soon_threadsafe(queue.put_nowait, ("error", "upstream_error", str(exc)))
            except RuntimeError as exc:
                loop.call_soon_threadsafe(queue.put_nowait, ("error", "not_signed_in", str(exc)))
            except Exception as exc:  # noqa: BLE001
                loop.call_soon_threadsafe(queue.put_nowait, ("error", "internal_error", str(exc)))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None, None))

        pump_task = loop.run_in_executor(None, pump)

        try:
            # --- SSE envelope open ---
            item_id = f"msg_{uuid.uuid4().hex}"
            yield _sse_responses_event("response.created", resp_id, created, "in_progress", model_name, [])
            yield _sse_responses_event("response.in_progress", resp_id, created, "in_progress", model_name, [])

            yield _sse_responses_event("response.output_item.added", resp_id, created, "in_progress",
                                       model_name, [], output_index=0, item_id=item_id,
                                       item_type="message", item_status="in_progress", role="assistant")
            yield _sse_responses_event("response.content_part.added", resp_id, created, "in_progress",
                                       model_name, [], item_id=item_id, output_index=0,
                                       content_index=0, part_type="output_text", part_text="")

            # --- stream deltas as they arrive ---
            deltas: list[str] = []
            while True:
                item = await queue.get()
                kind = item[0]

                if kind == "delta":
                    chunk = item[1]
                    deltas.append(chunk)
                    yield _sse_responses_event("response.output_text.delta", resp_id, created,
                                               "in_progress", model_name, [],
                                               item_id=item_id, output_index=0, content_index=0,
                                               delta=chunk)
                    continue

                if kind == "error":
                    _, err_type, err_msg = item
                    final = "".join(deltas)
                    yield format_stream_error_response(502, err_type, err_msg)
                    return

                if kind == "done":
                    break

            # --- SSE envelope close ---
            final_text = result_final_text or "".join(deltas)
            yield _sse_responses_event("response.output_text.done", resp_id, created,
                                       "completed", model_name, [],
                                       item_id=item_id, output_index=0, content_index=0,
                                       text=final_text)
            yield _sse_responses_event("response.content_part.done", resp_id, created,
                                       "completed", model_name, [],
                                       item_id=item_id, output_index=0, content_index=0,
                                       part_type="output_text", part_text=final_text)
            yield _sse_responses_event("response.output_item.done", resp_id, created,
                                       "completed", model_name, [],
                                       output_index=0, item_id=item_id,
                                       item_type="message", item_status="completed", role="assistant",
                                       content_text=final_text)
            yield _sse_responses_event("response.completed", resp_id, created,
                                       "completed", model_name, [],
                                       final_output=True, output_index=0, item_id=item_id,
                                       item_type="message", item_status="completed", role="assistant",
                                       content_text=final_text, conversation_id=conv_id_ref[0])
        finally:
            await pump_task


def _sse_responses_event(event_name: str, resp_id: str, created: int, status: str,
                          model_name: str, output: list, **kw) -> str:
    """Build one Responses API SSE event for the streaming path.

    Normalises the response envelope + various polymorphic inner objects
    (output item, content part, delta, done) from **kw so the caller doesn't
    repeat the same boilerplate for every event type.
    """
    # Base response envelope shared by all events.
    base = {"id": resp_id, "object": "response", "created_at": created,
            "status": status, "model": model_name, "output": output}

    # Build the data payload depending on event type.
    if event_name in ("response.created", "response.in_progress"):
        payload = {"type": event_name, "response": {**base}}

    elif event_name == "response.output_item.added":
        payload = {
            "type": event_name, "response": {**base},
            "output_index": kw.get("output_index", 0),
            "item": {"id": kw["item_id"], "type": kw.get("item_type", "message"),
                     "status": kw.get("item_status", "in_progress"),
                     "role": kw.get("role", "assistant"), "content": []},
        }

    elif event_name == "response.content_part.added":
        payload = {
            "type": event_name, "response": {**base},
            "item_id": kw["item_id"], "output_index": kw.get("output_index", 0),
            "content_index": kw.get("content_index", 0),
            "part": {"type": kw.get("part_type", "output_text"),
                     "text": kw.get("part_text", ""), "annotations": []},
        }

    elif event_name == "response.output_text.delta":
        payload = {
            "type": event_name, "response": {**base},
            "item_id": kw["item_id"], "output_index": kw.get("output_index", 0),
            "content_index": kw.get("content_index", 0),
            "delta": kw.get("delta", ""),
        }

    elif event_name == "response.output_text.done":
        payload = {
            "type": event_name, "response": {**base},
            "item_id": kw["item_id"], "output_index": kw.get("output_index", 0),
            "content_index": kw.get("content_index", 0),
            "text": kw.get("text", ""),
        }

    elif event_name == "response.content_part.done":
        payload = {
            "type": event_name, "response": {**base},
            "item_id": kw["item_id"], "output_index": kw.get("output_index", 0),
            "content_index": kw.get("content_index", 0),
            "part": {"type": kw.get("part_type", "output_text"),
                     "text": kw.get("part_text", ""), "annotations": []},
        }

    elif event_name == "response.output_item.done":
        payload = {
            "type": event_name, "response": {**base},
            "output_index": kw.get("output_index", 0),
            "item": {"id": kw["item_id"], "type": kw.get("item_type", "message"),
                     "status": kw.get("item_status", "completed"),
                     "role": kw.get("role", "assistant"),
                     "content": [{"type": "output_text", "text": kw.get("content_text", ""),
                                  "annotations": []}]},
        }

    elif event_name == "response.completed":
        payload = {
            "type": event_name, "response": {**base,
                "status": "completed",
                "output": [{"id": kw["item_id"], "type": kw.get("item_type", "message"),
                           "status": kw.get("item_status", "completed"),
                           "role": kw.get("role", "assistant"),
                           "content": [{"type": "output_text", "text": kw.get("content_text", ""),
                                        "annotations": []}]}],
                "conversation_id": kw.get("conversation_id"),
            },
        }

    else:
        payload = {"type": event_name, "response": {**base}}

    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def format_stream_error_response(status: int, kind: str, message: str) -> str:
    """One-off SSE event to surface an error mid-stream."""
    payload = {
        "type": "response.failed",
        "sequence_number": 0,
        "response": {
            "status": "failed",
            "error": {"type": kind, "code": status, "message": message},
        },
    }
    return f"event: response.failed\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
