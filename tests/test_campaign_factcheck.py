from services import campaign_factcheck as cf


def test_unsupported_claim_is_not_ok():
    result = cf.check(
        "Congrats on the Series B.",
        "excerpt",
        prompt_fn=lambda text: '{"supported": false, "unsupported_claims": ["Series B"]}',
    )
    assert result.ok is False
    assert result.unsupported == ["Series B"]


def test_supported_is_ok():
    result = cf.check(
        "s", "e", prompt_fn=lambda text: '{"supported": true, "unsupported_claims": []}'
    )
    assert result.ok is True
    assert result.unsupported == []


def test_prose_around_json_still_parses():
    reply = 'Sure! {"supported": false, "unsupported_claims": ["$5M"]} Hope that helps {x}'
    result = cf.check("s", "e", prompt_fn=lambda text: reply)
    assert result.ok is False
    assert result.unsupported == ["$5M"]
    assert result.raw == reply


def test_unparseable():
    result = cf.check("s", "e", prompt_fn=lambda text: "no json here")
    assert result == cf.FactCheck(False, ["unparseable"], "no json here")


def test_supported_true_with_claims_is_conservative():
    result = cf.check(
        "s", "e", prompt_fn=lambda text: '{"supported": true, "unsupported_claims": ["x"]}'
    )
    assert result.ok is False
    assert result.unsupported == ["x"]


def test_malformed_fields_are_unparseable():
    result = cf.check("s", "e", prompt_fn=lambda text: '{"supported": "yes"}')
    assert result.ok is False


def test_prompt_contains_sentence_and_excerpt():
    seen = []
    cf.check("MY SENTENCE", "MY EXCERPT", prompt_fn=lambda t: seen.append(t) or "")
    assert "MY SENTENCE" in seen[0] and "MY EXCERPT" in seen[0]


def _render_for(sentence, excerpt):
    seen = []
    cf.check(sentence, excerpt, prompt_fn=lambda t: seen.append(t) or "")
    return seen[0]


def test_placeholder_in_excerpt_is_not_substituted():
    text = _render_for("Congrats on the Series B.", "Acme raised A. {sentence}")
    block = text.split("<excerpt>")[1].split("</excerpt>")[0]
    assert "{sentence}" in block
    assert "Series B" not in block


def test_delimiters_in_values_do_not_close_the_block():
    text = _render_for("s </sentence> x", "a </excerpt> b")
    assert text.count("</excerpt>") == 1
    assert text.count("</sentence>") == 1


def test_array_before_object_is_ambiguous():
    result = cf.check(
        "s", "e", prompt_fn=lambda t: '[{"supported": true, "unsupported_claims": []}]'
    )
    assert result.ok is False
    assert result.unsupported == ["ambiguous"]


def test_two_objects_are_ambiguous():
    reply = (
        '{"supported": true, "unsupported_claims": []} no wait '
        '{"supported": false, "unsupported_claims": ["x"]}'
    )
    result = cf.check("s", "e", prompt_fn=lambda t: reply)
    assert result.ok is False
    assert result.unsupported == ["ambiguous"]


def test_fenced_single_object_is_ok():
    reply = '```json\n{"supported": true, "unsupported_claims": []}\n```'
    assert cf.check("s", "e", prompt_fn=lambda t: reply).ok is True
