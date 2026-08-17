"""Start the OpenAI-compatible M365 Copilot server.

    python app.py            # http://127.0.0.1:8000
    HOST=0.0.0.0 PORT=8080 python app.py
"""

from server import app

if __name__ == "__main__":
    app()
