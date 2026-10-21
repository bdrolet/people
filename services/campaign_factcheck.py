"""Fact-check the opening sentence of a campaign draft against the source excerpt,
in a separate `claude -p` call that shares no context with the writer."""

import json
from collections.abc import Callable
from dataclasses import dataclass

from clients import claude_cli

PROMPT_VERSION = "2026-10-21"

PROMPT = """You are checking one sentence against a source excerpt.

A claim is supported only if the excerpt states it. Do not accept claims that are
merely implied or plausible. List every factual claim in the sentence that the
excerpt does not state (round, amount, product, customers, dates, names).

Reply with exactly one JSON object and nothing else:
{"supported": <true|false>, "unsupported_claims": ["..."]}

Source excerpt:
<excerpt>
{excerpt}
</excerpt>

Sentence:
<sentence>
{sentence}
</sentence>
"""


@dataclass(frozen=True)
class FactCheck:
    ok: bool
    unsupported: list[str]
    raw: str


def _parse(raw: str) -> dict | None:
    start = raw.find("{")
    while start != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(raw, start)
        except ValueError:
            start = raw.find("{", start + 1)
            continue
        return obj if isinstance(obj, dict) else None
    return None


def check(
    opening_sentence: str,
    excerpt: str,
    *,
    prompt_fn: Callable[[str], str] = claude_cli.prompt,
) -> FactCheck:
    text = PROMPT.replace("{excerpt}", excerpt).replace("{sentence}", opening_sentence)
    raw = prompt_fn(text)
    obj = _parse(raw)
    claims = obj.get("unsupported_claims") if obj else None
    if obj is None or not isinstance(obj.get("supported"), bool) or not isinstance(claims, list):
        return FactCheck(False, ["unparseable"], raw)
    unsupported = [str(c) for c in claims]
    return FactCheck(obj["supported"] is True and not unsupported, unsupported, raw)
