"""M365 Copilot Sydney Chathub protocol constants + payload builders.

Reverse-engineered from a captured web-client session (see
``captures/chat_frames.md``). Two things live here:

  * URL and framing constants (SignalR JSON hub over WS).
  * :func:`build_invocation` — the giant ``type:4`` payload the client sends
    to invoke the ``chat`` target. Every field is copied from the real client
    because unknown/omitted fields tend to fail closed on Substrate.

If Microsoft rev's the wire format, re-capture with ``tools/capture_m365.py``
and diff against ``chat_frames.md``.
"""

from __future__ import annotations

import time
import uuid
from typing import Dict, List, Optional
from urllib.parse import quote


# -- SignalR framing ---------------------------------------------------------

# JSON hub record separator. A single WebSocket packet may carry multiple
# JSON frames concatenated with 0x1E; drivers must split on it.
RECORD_SEPARATOR = "\x1e"

# SignalR message types we care about.
MSG_INVOCATION = 1          # server → client streaming update
MSG_COMPLETION = 2          # server → client final result (with conversationId)
MSG_COMPLETION_ACK = 3      # server → client "invocation done"
MSG_INVOKE = 4              # client → server invocation (send a message)
MSG_PING = 6                # keep-alive


# -- URL ---------------------------------------------------------------------

CHATHUB_HOST = "wss://substrate.office.com/m365Copilot/Chathub"


def build_chathub_url(
    object_id: str,
    tenant_id: str,
    access_token: str,
    conversation_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Return the WSS URL for a new Chathub connection.

    ``object_id`` and ``tenant_id`` come from the Sydney JWT payload (oid/tid).
    ``session_id`` is a per-connection UUID; a fresh one is minted per call
    when omitted. ``conversation_id`` is optional for the very first turn
    (server assigns one and returns it in the completion frame); pass it back
    on subsequent turns to continue the same conversation.
    """
    sid = session_id or str(uuid.uuid4())
    params = {
        "chatsessionid": sid,
        "XRoutingParameterSessionKey": sid,
        "clientrequestid": sid,
        "X-SessionId": sid,
        "access_token": access_token,
        "source": '"officeweb"',
        "product": "Office",
        "agentHost": "Bizchat.FullScreen",
        "licenseType": "Starter",
        "isEdu": "false",
        "agent": "web",
        "scenario": "OfficeWebIncludedCopilot",
    }
    if conversation_id:
        params["ConversationId"] = conversation_id
    query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
    return f"{CHATHUB_HOST}/{object_id}@{tenant_id}?{query}"


# -- invocation payload ------------------------------------------------------

# Feature flags the web client toggles on. Sending an empty list works for
# plain text turns but disables citations/rich responses/streaming niceties.
# Copied verbatim from a real capture; safe to prune when we understand which
# ones the server actually requires.
DEFAULT_OPTIONS_SETS: List[str] = [
    "search_result_progress_messages_with_search_queries",
    "update_textdoc_response_after_streaming",
    "deepleo_networking_timeout_10minutes_canmore",
    "cwc_flux_image",
    "cwc_code_interpreter",
    "cwc_code_interpreter_amsfix",
    "cwcfluxgptv",
    "flux_v3_gptv_enable_upload_multi_image_in_turn_wo_ch",
    "gptvnorm2048",
    "cwc_code_interpreter_citation_fix",
    "code_interpreter_interactive_charts",
    "cwc_code_interpreter_interactive_charts_inline_image",
    "code_interpreter_matplotlib_patching",
    "cwc_fileupload_odb",
    "update_memory_plugin",
    "add_custom_instructions",
    "cwc_flux_v3",
    "flux_v3_progress_messages",
    "enable_batch_token_processing",
    "enable_gg_gpt",
    "enable_inferred_memory_read",
    "rich_responses",
    "pages_citations",
    "pages_citations_multiturn",
]

DEFAULT_ALLOWED_MESSAGE_TYPES: List[str] = [
    "Chat",
    "Suggestion",
    "InternalSearchQuery",
    "Disengaged",
    "InternalLoaderMessage",
    "Progress",
    "GeneratedCode",
    "RenderCardRequest",
    "AdsQuery",
    "SemanticSerp",
    "GenerateContentQuery",
    "GenerateGraphicArt",
    "SearchQuery",
    "ConfirmationCard",
    "AuthError",
    "DeveloperLogs",
    "TriggerPlugin",
    "HintInvocation",
    "MemoryUpdate",
    "EndOfRequest",
    "TriggerConfirmation",
    "ResumeInvokeAction",
    "ResumeUserInputRequest",
    "TriggerUserInputRequest",
    "EscapeHatch",
    "TriggerPluginAuth",
    "ResumePluginAuth",
    "SideBySide",
    "ReferencesListComplete",
]


def _hex32() -> str:
    """Return a 32-char lowercase hex id, matching the web client's format."""
    return uuid.uuid4().hex


# The M365 Copilot ``tone`` field selects the backend model. Values are
# reverse-engineered from real client captures — Microsoft ships new tone
# strings whenever a new frontier model becomes GA in Copilot, so this list
# is a snapshot, not a spec.
#
#   * ``Magic``             — the "smart routing" default (auto-picks a model)
#   * ``Precise`` / ``Creative`` / ``Balanced`` — legacy Bing-Chat tones
#   * ``Gpt_5_6_Reasoning`` — GPT-5.6 Thinker (deeper reasoning; slower)
#
# We default to Thinker because that's what you asked for. Callers can pass
# a different ``tone`` to ``build_invocation`` when they want the fast path.
TONE_MAGIC = "Magic"
TONE_THINKER = "Gpt_5_6_Reasoning"
DEFAULT_TONE = TONE_THINKER


def build_invocation(
    prompt: str,
    session_id: str,
    request_id: Optional[str] = None,
    locale: str = "zh-cn",
    time_zone: str = "Asia/Shanghai",
    time_zone_offset: int = 8,
    options_sets: Optional[List[str]] = None,
    allowed_message_types: Optional[List[str]] = None,
    is_start_of_session: bool = True,
    tone: str = DEFAULT_TONE,
    plugins: Optional[List[Dict]] = None,
    invocation_id: str = "0",
) -> Dict:
    """Assemble a full ``type:4`` invocation frame for the ``chat`` target."""
    req_id = request_id or _hex32()
    return {
        "arguments": [{
            "source": "officeweb",
            "clientCorrelationId": req_id,
            "sessionId": session_id,
            "traceId": req_id,
            "isStartOfSession": is_start_of_session,
            "optionsSets": options_sets if options_sets is not None else DEFAULT_OPTIONS_SETS,
            "allowedMessageTypes": (
                allowed_message_types
                if allowed_message_types is not None
                else DEFAULT_ALLOWED_MESSAGE_TYPES
            ),
            "streamingMode": "ConciseWithPadding",
            "options": {},
            "extraExtensionParameters": {},
            "sliceIds": [],
            "threadLevelGptId": {},
            "clientInfo": {
                "clientPlatform": "mcmcopilot-web",
                "clientAppName": "Office",
                "clientEntrypoint": "mcmcopilot-officeweb",
                "clientSessionId": session_id,
                "ProductCategory": "Chat",
                "clientAppType": "Web",
                "productEntryPoint": "ChatPanel",
                "deviceOS": "macOS",
                "deviceType": "Desktop",
                "clientPlatformVersion": "10.15.7",
            },
            "message": {
                "author": "user",
                "inputMethod": "Keyboard",
                "text": prompt,
                "entityAnnotationTypes": ["People", "File", "Event", "Email", "TeamsMessage"],
                "requestId": req_id,
                "locationInfo": {
                    "timeZoneOffset": time_zone_offset,
                    "timeZone": time_zone,
                },
                "locale": locale,
                "messageType": "Chat",
                "experienceType": "Default",
                "adaptiveCards": [],
                "clientPreferences": {},
                "connectedFederatedConnections": ["dummyId"],
            },
            "plugins": plugins if plugins is not None else [{"Id": "BingWebSearch", "Source": "BuiltIn"}],
            "isSbsSupported": True,
            "tone": tone,
            "renderReferencesBehindEOS": True,
            "disconnectBehavior": "continue",
        }],
        "invocationId": invocation_id,
        "target": "chat",
        "type": MSG_INVOKE,
    }


def build_metrics_frame(timestamps: Dict[str, str]) -> Dict:
    """Optional telemetry frame the web client sends. Server ignores content."""
    return {
        "arguments": [{"Timestamps": timestamps}],
        "target": "Metrics",
        "type": MSG_INVOCATION,
    }


# -- helpers -----------------------------------------------------------------

def now_iso_ms() -> str:
    """Sub-second-precision ISO timestamp like ``2026-07-15T12:01:57.944Z``.

    Matches the format the web client uses in its Metrics timestamps.
    """
    t = time.time()
    ms = int((t - int(t)) * 1000)
    struct = time.gmtime(int(t))
    return time.strftime("%Y-%m-%dT%H:%M:%S", struct) + f".{ms:03d}Z"
