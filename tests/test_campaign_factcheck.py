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
