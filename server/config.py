"""Server configuration — env-driven, sensible defaults."""

import os

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))

# Self-imposed rate limit: how many requests per minute the bridge accepts
# before returning 429. 0 disables limiting. Sydney publishes no consumer-facing
# rate limit for M365 Copilot, but hammering it will burn your daily
# TenantDataAccess / LLMOnly quotas — see the throttling.metering block on any
# turn's completion frame.
RATE_LIMIT_RPM = int(os.environ.get("RATE_LIMIT_RPM", "20"))
RATE_LIMIT_BURST = int(os.environ.get("RATE_LIMIT_BURST", "5"))

# The single advertised model name. Client-side "model" parameter is ignored
# — every request lands on the same Copilot Deep Thinker tone on the backend.
# The id matches what /v1/models announces (see api.CANONICAL_MODEL_NAME) so
# OpenAI-compatible clients see one consistent name across listing +
# response payloads.
MODEL_NAME = os.environ.get("MODEL_NAME", "m365-copilot")
