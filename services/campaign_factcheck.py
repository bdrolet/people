"""Fact-check the opening sentence of a campaign draft against the source excerpt,
in a separate `claude -p` call that shares no context with the writer."""

import json
import re
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


def _objects(raw: str) -> list[dict]:
    """Every top-level JSON object in `raw`, in order."""
    found = []
    decoder = json.JSONDecoder()
    pos = raw.find("{")
    while pos != -1:
        try:
            obj, end = decoder.raw_decode(raw, pos)
        except ValueError:
            pos = raw.find("{", pos + 1)
            continue
        found.append(obj)
        pos = raw.find("{", end)
    return found


def _render(opening_sentence: str, excerpt: str) -> str:
    vals = {
        "excerpt": excerpt.replace("</excerpt>", "< /excerpt>"),
        "sentence": opening_sentence.replace("</sentence>", "< /sentence>"),
    }
    return re.sub(r"\{(excerpt|sentence)\}", lambda m: vals[m.group(1)], PROMPT)


def check(
    opening_sentence: str,
    excerpt: str,
    *,
    prompt_fn: Callable[[str], str] = claude_cli.prompt,
) -> FactCheck:
    raw = prompt_fn(_render(opening_sentence, excerpt))
    objects = _objects(raw)
    first_brace = raw.find("{")
    if len(objects) > 1 or (objects and "[" in raw[:first_brace]):
        return FactCheck(False, ["ambiguous"], raw)
    obj = objects[0] if objects else None
    claims = obj.get("unsupported_claims") if obj else None
    if obj is None or not isinstance(obj.get("supported"), bool) or not isinstance(claims, list):
        return FactCheck(False, ["unparseable"], raw)
    unsupported = [str(c) for c in claims]
    return FactCheck(obj["supported"] is True and not unsupported, unsupported, raw)
