"""clients/linkedin_mcp.py's pure result normalization. Never starts the real MCP."""

from types import SimpleNamespace

from mcp.types import CallToolResult, TextContent

from clients import linkedin_mcp


def _result(**kw):
    return CallToolResult(content=kw.pop("content", []), **kw)


def test_normalize_error_result_with_retry_after():
    r = _result(
        is_error=True,
        content=[TextContent(type="text", text="slow down")],
        meta={"linkedin.dev/error": {"v": 1, "code": "rate_limited", "retry_after": 90}},
    )
    assert linkedin_mcp.normalize_call_result(r) == {
        "error": {"code": "rate_limited", "retry_after": 90}
    }


def test_normalize_error_without_marker_is_internal():
    r = _result(is_error=True, content=[TextContent(type="text", text="boom")])
    assert linkedin_mcp.normalize_call_result(r) == {
        "error": {"code": "internal", "retry_after": None}
    }


def test_normalize_structured_result():
    body = {"status": "sent", "sent": True, "retry_safe": False, "thread_id": "t"}
    assert linkedin_mcp.normalize_call_result(_result(structured_content=body)) == body


def test_normalize_wrapped_structured_result():
    body = {"status": "sent", "sent": True}
    r = _result(structured_content={"result": body})
    assert linkedin_mcp.normalize_call_result(r) == body


def test_normalize_text_json_result():
    r = _result(content=[TextContent(type="text", text='{"status": "composer_occupied"}')])
    assert linkedin_mcp.normalize_call_result(r) == {"status": "composer_occupied"}


def test_normalize_unreadable_result_is_outcome_unknown():
    r = _result(content=[TextContent(type="text", text="not json")])
    assert linkedin_mcp.normalize_call_result(r) == {
        "error": {"code": "outcome_unknown", "retry_after": None}
    }


def test_session_problem():
    assert linkedin_mcp.session_problem({"session": {"stored": True}}) is None
    assert linkedin_mcp.session_problem({"session": {"stored": False}}) == "login"
    assert linkedin_mcp.session_problem({"error": {"code": "internal"}}) == "login"
    assert linkedin_mcp.session_problem(SimpleNamespace()) == "login"  # type: ignore[arg-type]
    restricted = {"session": {"stored": True}, "account_restricted": True}
    assert linkedin_mcp.session_problem(restricted) == "account_restricted"


def test_server_args_pin_the_version():
    assert linkedin_mcp.server_args("4.29.0") == ["mcp-server-linkedin==4.29.0"]
