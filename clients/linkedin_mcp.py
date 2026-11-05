"""The LinkedIn MCP server over stdio, for scripts/linkedin_sender.py only. Local: people's
Cloud Functions never import this.

The sender pins the server version (LINKEDIN_MCP_VERSION, `uvx mcp-server-linkedin==<v>`)
because services/linkedin_result.py is written against that version's send_message
contract. Claude sessions keep running @latest through .mcp.json."""

import contextlib
import json
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

from anyio.from_thread import BlockingPortal, start_blocking_portal
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.types import CallToolResult

ERROR_META_KEY = "linkedin.dev/error"
SEND_TIMEOUT_SECONDS = 240.0
STATUS_TIMEOUT_SECONDS = 60.0


def server_args(version: str) -> list[str]:
    return [f"mcp-server-linkedin=={version}"]


def _unknown() -> dict:
    return {"error": {"code": "outcome_unknown", "retry_after": None}}


def normalize_call_result(result: CallToolResult) -> dict:
    """The tool's dict, or {"error": {"code", "retry_after"}} for an error result (the
    code from `_meta["linkedin.dev/error"]`). A success that can't be read is
    outcome_unknown: only LinkedIn can say whether that send left."""
    if result.is_error:
        marker = (result.meta or {}).get(ERROR_META_KEY) or {}
        return {
            "error": {
                "code": str(marker.get("code") or "internal"),
                "retry_after": marker.get("retry_after"),
            }
        }
    body: Any = result.structured_content
    if isinstance(body, dict) and set(body) == {"result"} and isinstance(body["result"], dict):
        body = body["result"]
    if isinstance(body, dict):
        return body
    for block in result.content:
        text = getattr(block, "text", None)
        if text is None:
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            return _unknown()
        return parsed if isinstance(parsed, dict) else _unknown()
    return _unknown()


def session_problem(status: Any) -> str | None:
    """Why the session can't be used, or None. get_session_status reads stored files only
    (no browser, no login); a background run never proceeds without a stored session,
    because it must not start a login on Ben's screen."""
    if not isinstance(status, dict):
        return "login"
    if status.get("account_restricted") is True:
        return "account_restricted"
    session = status.get("session")
    if not (isinstance(session, dict) and session.get("stored") is True):
        return "login"
    return None


class McpSender:
    """services.linkedin_outbox.Sender over one MCP session, driven from sync code."""

    def __init__(self, portal: BlockingPortal, session: ClientSession) -> None:
        self._portal = portal
        self._session = session

    def _call(self, name: str, arguments: dict, timeout: float) -> dict:
        result = self._portal.call(
            lambda: self._session.call_tool(name, arguments, read_timeout_seconds=timeout)
        )
        return normalize_call_result(result)

    def session_problem(self) -> str | None:
        return session_problem(self._call("get_session_status", {}, STATUS_TIMEOUT_SECONDS))

    def send(self, username: str, message: str, profile_urn: str | None) -> dict:
        args: dict[str, Any] = {
            "linkedin_username": username,
            "message": message,
            "confirm_send": True,
        }
        if profile_urn:
            args["profile_urn"] = profile_urn
        return self._call("send_message", args, SEND_TIMEOUT_SECONDS)


@contextlib.asynccontextmanager
async def _session(params: StdioServerParameters) -> AsyncIterator[ClientSession]:
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


@contextlib.contextmanager
def open_sender(version: str) -> Iterator[McpSender]:
    params = StdioServerParameters(
        command=os.environ.get("UVX", "uvx"),
        args=server_args(version),
        env={"UV_HTTP_TIMEOUT": "300"},
    )
    with start_blocking_portal() as portal:
        with portal.wrap_async_context_manager(_session(params)) as session:
            yield McpSender(portal, session)
