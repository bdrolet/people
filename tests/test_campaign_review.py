import re
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from services import campaign_review as cr

KEY = "k" * 32


def _entry(contact_id, *, body="Hi there.\nLine two.", flags=(), reason=None):
    entry = {
        "contact": {
            "id": contact_id,
            "full_name": f"Person {contact_id}",
            "title": "CTO",
            "linkedin_url": f"https://www.linkedin.com/in/person-{contact_id}",
            "email": f"p{contact_id}@example.com",
            "verification": "valid",
            "status": "drafted",
            "subject": f"Subject {contact_id}",
            "body": body,
            "opener_generic": "generic opener" in flags,
        },
        "company": {
            "name": f"Company {contact_id}",
            "domain": f"c{contact_id}.example",
            "round": "Series A",
            "product_summary": "Builds things",
            "fit_score": 8,
            "fit_notes": "Good fit",
        },
        "flags": list(flags),
    }
    if reason is not None:
        entry["reason"] = reason
    return entry


def _page(drafted=None, parked=None):
    return cr.render_page(
        drafted=drafted if drafted is not None else [_entry(7), _entry(8)],
        parked=parked
        if parked is not None
        else {
            "unverified": [_entry(20, reason="no verified email")],
            "skipped_warm": [_entry(21, reason="already in your contacts")],
            "below_bar": [_entry(22, reason="fit score 3 below bar")],
        },
        key=KEY,
    )


def test_one_checked_checkbox_per_drafted_row_none_for_parked():
    html = _page()
    assert re.findall(r'name="approve_(\d+)"', html) == ["7", "8"]
    for cid in (7, 8):
        assert re.search(rf'<input type="checkbox" name="approve_{cid}"[^>]*checked', html)
        assert f'<textarea name="body_{cid}"' in html
    for cid in (20, 21, 22):
        assert f"approve_{cid}" not in html
        assert f"body_{cid}" not in html


def test_drafted_row_content():
    html = _page()
    for text in (
        "Company 7",
        "Series A",
        "Builds things",
        "Good fit",
        "Person 7",
        "CTO",
        "p7@example.com",
        "Subject 7",
        'href="https://www.linkedin.com/in/person-7"',
    ):
        assert text in html


def test_single_form_and_button():
    html = _page()
    assert html.count("<form") == 1
    assert f'<form method="post" action="/approve?key={KEY}">' in html
    assert html.count("<button") == 1
    assert "Send ticked" in html


def test_flag_badges():
    html = _page(
        drafted=[_entry(7, flags=["generic opener", "fact check regenerated"]), _entry(8)],
        parked={},
    )
    assert "generic opener" in html
    assert "fact check regenerated" in html


def test_no_badges_when_unflagged():
    html = _page(drafted=[_entry(7)], parked={})
    assert "generic opener" not in html
    assert "fact check regenerated" not in html


def test_parked_reasons_and_headings():
    html = _page()
    for text in (
        "no verified email",
        "already in your contacts",
        "fit score 3 below bar",
        "Person 20",
        "Company 22",
    ):
        assert text in html


def test_escapes_scraped_values():
    evil = _entry(7, body="<script>alert(1)</script>")
    evil["company"]["fit_notes"] = "<b>bold</b>"
    evil["contact"]["full_name"] = 'Eve "<img>"'
    html = _page(drafted=[evil], parked={})
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<b>bold</b>" not in html
    assert "<img>" not in html


def test_no_external_urls_other_than_linkedin():
    html = _page()
    urls = re.findall(r"https?://[^\s\"'<>]+", html)
    assert urls
    assert all(u.startswith("https://www.linkedin.com/") for u in urls)


def test_parse_approvals_unchanged_body_is_none():
    entry_body = "Hi there.\nLine two."
    form = {
        "approve_7": ["on"],
        "body_7": [entry_body.replace("\n", "\r\n")],
        "original_7": [entry_body],
    }
    assert cr.parse_approvals(form, [7]) == [(7, "approved", None)]


def test_parse_approvals_missing_checkbox_is_skipped():
    form = {"body_8": ["x"], "original_8": ["x"]}
    assert cr.parse_approvals(form, [8]) == [(8, "skipped", None)]


def test_parse_approvals_edited_body():
    form = {"approve_7": ["on"], "body_7": ["New\r\ntext"], "original_7": ["Old"]}
    assert cr.parse_approvals(form, [7]) == [(7, "approved", "New\ntext")]


def test_parse_approvals_ignores_ids_not_drafted():
    form = {"approve_7": ["on"], "approve_99": ["on"], "body_7": ["a"], "original_7": ["a"]}
    assert cr.parse_approvals(form, [7]) == [(7, "approved", None)]


def test_render_then_parse_roundtrip_unedited():
    html = _page(drafted=[_entry(7)], parked={})
    # Pull the hidden original back out as a browser would submit it.
    import html as html_lib

    original = html_lib.unescape(re.search(r'name="original_7" value="([^"]*)"', html).group(1))
    form = {"approve_7": ["on"], "body_7": ["Hi there.\r\nLine two."], "original_7": [original]}
    assert cr.parse_approvals(form, [7]) == [(7, "approved", None)]


def _start(**kwargs):
    port_holder = {}
    listening = threading.Event()

    def on_listening(port):
        port_holder["port"] = port
        listening.set()

    calls = []
    kwargs.setdefault("on_submit", calls.append)
    thread = threading.Thread(
        target=cr.serve,
        kwargs=dict(open_browser=False, on_listening=on_listening, **kwargs),
        daemon=True,
    )
    thread.start()
    assert listening.wait(5)
    return thread, port_holder["port"], calls


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.read().decode()


def test_serve_full_flow(capsys):
    thread, port, calls = _start(
        html="<html>PAGE</html>", key=KEY, drafted_ids=[7, 8], idle_timeout_s=5
    )
    base = f"http://127.0.0.1:{port}"

    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(f"{base}/?key=wrong")
    assert exc.value.code == 403
    assert exc.value.read() == b""
    exc.value.close()

    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(f"{base}/")
    assert exc.value.code == 403
    exc.value.close()

    status, body = _get(f"{base}/?key={KEY}")
    assert status == 200
    assert "PAGE" in body

    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(
            urllib.request.Request(f"{base}/approve?key=wrong", data=b"approve_7=on"), timeout=5
        )
    assert exc.value.code == 403
    exc.value.close()
    assert calls == []

    data = urllib.parse.urlencode(
        {"approve_7": "on", "body_7": "edited", "original_7": "orig", "body_8": "b"}
    ).encode()
    req = urllib.request.Request(f"{base}/approve?key={KEY}", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        text = resp.read().decode()
    assert "Sent 1, skipped 1" in text

    thread.join(5)
    assert not thread.is_alive()
    assert calls == [[(7, "approved", "edited"), (8, "skipped", None)]]
    assert f"http://127.0.0.1:{port}/?key={KEY}" in capsys.readouterr().out


def test_serve_idle_timeout_exits_without_submit():
    thread, _port, calls = _start(html="x", key=KEY, drafted_ids=[7], idle_timeout_s=1)
    thread.join(5)
    assert not thread.is_alive()
    assert calls == []
