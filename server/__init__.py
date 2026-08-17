"""OpenAI-compatible FastAPI server on top of M365 Copilot Chathub.

    python app.py            # http://127.0.0.1:8000
    HOST=0.0.0.0 PORT=8080 python app.py

Endpoints:
    POST /v1/chat/completions   — OpenAI Chat Completions (stream & non-stream)
    GET  /v1/models             — advertises a single "m365-copilot" model
    GET  /healthz               — liveness probe
"""

from .config import HOST, PORT
from .api import app as asgi_app


def app() -> None:
    """Run uvicorn on the ASGI app. Called by ``app.py``."""
    import uvicorn

    print(f"M365 Copilot OpenAI-compatible API on http://{HOST}:{PORT}", flush=True)
    uvicorn.run(asgi_app, host=HOST, port=PORT, log_level="info")


__all__ = ["app", "asgi_app"]
