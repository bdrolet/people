"""The weekly approval step: render the drafted campaign rows as one self-contained
HTML page and serve it on a short-lived loopback server until the form is posted.

This module never touches the database; the caller's `on_submit` applies the result.
"""

import html as html_lib
import http.server
import logging
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable

log = logging.getLogger(__name__)

Approval = tuple[int, str, str | None]

PARKED_HEADINGS = (
    ("unverified", "Unverified email"),
    ("skipped_warm", "Skipped: already warm"),
    ("below_bar", "Below the fit bar"),
)

CSS = """
body { font-family: -apple-system, system-ui, sans-serif; margin: 0 auto; max-width: 960px;
  padding: 16px; color: #1a1a1a; background: #fafafa; }
h1 { font-size: 1.4em; } h2 { font-size: 1.15em; margin-top: 2em; }
.row { background: #fff; border: 1px solid #ddd; border-radius: 6px; padding: 12px 16px;
  margin: 12px 0; }
.parked { color: #555; background: #f3f3f3; }
.meta { color: #555; font-size: 0.9em; margin: 4px 0; }
.badge { display: inline-block; background: #fde7c4; color: #7a4a00; border-radius: 4px;
  padding: 1px 6px; font-size: 0.8em; margin-left: 6px; }
.reason { font-style: italic; }
.subject { font-weight: 600; margin: 8px 0 4px; }
textarea { width: 100%; box-sizing: border-box; min-height: 12em; font: inherit; }
label.approve { font-weight: 600; }
button { font-size: 1.1em; padding: 8px 20px; margin: 16px 0; }
"""


def _e(value) -> str:
    return html_lib.escape("" if value is None else str(value), quote=True)


def _who(contact: dict) -> str:
    name = _e(contact.get("full_name"))
    url = contact.get("linkedin_url")
    if url:
        name = f'<a href="{_e(url)}" target="_blank" rel="noopener noreferrer">{name}</a>'
    title = contact.get("title")
    return f"{name}, {_e(title)}" if title else name


def _company_line(company: dict) -> str:
    parts = [f"<strong>{_e(company.get('name'))}</strong>"]
    if company.get("round"):
        parts.append(_e(company["round"]))
    if company.get("fit_score") is not None:
        parts.append(f"fit {_e(company['fit_score'])}")
    return " &middot; ".join(parts)


def _drafted_row(entry: dict) -> str:
    contact, company = entry["contact"], entry["company"]
    cid = int(contact["id"])
    badges = "".join(f'<span class="badge">{_e(flag)}</span>' for flag in entry.get("flags", []))
    body = contact.get("body") or ""
    return f"""<div class="row">
<div>{_company_line(company)}{badges}</div>
<div class="meta">{_e(company.get("product_summary"))}</div>
<div class="meta">Fit notes: {_e(company.get("fit_notes"))}</div>
<div class="meta">{_who(contact)} &lt;{_e(contact.get("email"))}&gt;</div>
<label class="approve"><input type="checkbox" name="approve_{cid}" checked> Approve</label>
<div class="subject">Subject: {_e(contact.get("subject"))}</div>
<textarea name="body_{cid}">
{_e(body)}</textarea>
<input type="hidden" name="original_{cid}" value="{_e(body)}">
</div>"""


def _parked_row(entry: dict) -> str:
    contact, company = entry["contact"], entry["company"]
    return f"""<div class="row parked">
<div>{_company_line(company)}</div>
<div class="meta">{_who(contact)}</div>
<div class="meta reason">{_e(entry.get("reason"))}</div>
</div>"""


def render_page(*, drafted: list[dict], parked: dict[str, list[dict]], key: str) -> str:
    sections = [f"<h2>Drafted ({len(drafted)})</h2>"]
    sections += [_drafted_row(entry) for entry in drafted] or ["<p>Nothing drafted.</p>"]
    sections.append('<button type="submit">Send ticked</button>')
    for group, heading in PARKED_HEADINGS:
        rows = parked.get(group) or []
        if rows:
            sections.append(f"<h2>{_e(heading)} ({len(rows)})</h2>")
            sections += [_parked_row(entry) for entry in rows]
    action = f"/approve?key={urllib.parse.quote(key, safe='')}"
    body = "\n".join(sections)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Campaign approval</title>
<style>{CSS}</style>
</head>
<body>
<h1>Campaign approval</h1>
<form method="post" action="{_e(action)}">
{body}
</form>
</body>
</html>
"""


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_approvals(form: dict[str, list[str]], drafted_ids: list[int]) -> list[Approval]:
    """`(contact_id, "approved" | "skipped", edited_body_or_None)` per drafted id.

    A body equal to the original (ignoring CRLF vs LF, which browsers introduce) is None;
    a skipped row never carries an edit."""
    out: list[Approval] = []
    for cid in drafted_ids:
        if f"approve_{cid}" not in form:
            out.append((cid, "skipped", None))
            continue
        bodies = form.get(f"body_{cid}")
        edited = None
        if bodies:
            new = _normalize(bodies[0])
            original = _normalize((form.get(f"original_{cid}") or [""])[0])
            if new != original:
                edited = new
        out.append((cid, "approved", edited))
    return out


def _done_page(approvals: list[Approval]) -> str:
    sent = sum(1 for _, status, _ in approvals if status == "approved")
    skipped = len(approvals) - sent
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Done</title></head>'
        f"<body><p>Sent {sent}, skipped {skipped}. You can close this tab.</p></body></html>"
    )


def serve(
    *,
    html: str,
    key: str,
    drafted_ids: list[int],
    on_submit: Callable[[list[Approval]], None],
    idle_timeout_s: int = 1800,
    open_browser: bool = True,
    on_listening: Callable[[int], None] | None = None,
) -> None:
    """Serve `html` on a loopback port until the form is posted (calls `on_submit` once)
    or no valid request arrives for `idle_timeout_s` seconds (does not call it).

    Re-raises an exception from `on_submit` after shutting down."""
    state_lock = threading.Lock()
    finished = threading.Event()
    state: dict = {"last": time.monotonic(), "submitted": False, "error": None}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            log.debug("%s " + format, self.address_string(), *args)

        def _authorized(self) -> tuple[bool, str]:
            parsed = urllib.parse.urlsplit(self.path)
            keys = urllib.parse.parse_qs(parsed.query).get("key", [])
            ok = keys == [key]
            if not ok:
                self._send(403, b"")
            else:
                with state_lock:
                    state["last"] = time.monotonic()
            return ok, parsed.path

        def _send(self, status: int, body: bytes, content_type: str = "text/html") -> None:
            self.send_response(status)
            if body:
                self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            ok, path = self._authorized()
            if not ok:
                return
            if path != "/":
                self._send(404, b"")
                return
            self._send(200, html.encode("utf-8"))

        def do_POST(self):
            ok, path = self._authorized()
            if not ok:
                return
            if path != "/approve":
                self._send(404, b"")
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            form = urllib.parse.parse_qs(raw, keep_blank_values=True)
            with state_lock:
                if state["submitted"]:
                    self._send(409, b"Already submitted.", "text/plain")
                    return
                state["submitted"] = True
            approvals = parse_approvals(form, drafted_ids)
            try:
                on_submit(approvals)
            except BaseException as exc:
                state["error"] = exc
                self._send(500, b"Submit failed; see the terminal.", "text/plain")
                finished.set()
                return
            self._send(200, _done_page(approvals).encode("utf-8"))
            finished.set()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/?key={urllib.parse.quote(key, safe='')}"
    print(f"Approval page: {url}", flush=True)
    loop = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1})
    loop.daemon = True
    loop.start()
    try:
        if on_listening is not None:
            on_listening(port)
        if open_browser:
            webbrowser.open(url)
        while not finished.wait(0.1):
            with state_lock:
                idle = time.monotonic() - state["last"]
                submitting = state["submitted"]
            if idle >= idle_timeout_s and not submitting:
                log.info("approval page idle for %ss; exiting without sending", idle_timeout_s)
                break
    finally:
        server.shutdown()
        server.server_close()
        loop.join()
    if state["error"] is not None:
        raise state["error"]
