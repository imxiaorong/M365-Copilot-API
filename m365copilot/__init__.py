"""M365 Copilot Chat — local API bridge.

Public entry point is :class:`M365CopilotClient` from :mod:`m365copilot.client`.

    from m365copilot import M365CopilotClient
    client = M365CopilotClient()
    reply = client.chat("Hello")
    print(reply.text)
"""

from .client import M365CopilotClient, ChatReply, ChatStream

__all__ = ["M365CopilotClient", "ChatReply", "ChatStream"]
