from services.campaign_draft import (
    DEFAULT_SUBJECT,
    GENERIC_OPENER,
    render_email,
    render_follow_up,
    valid_subject,
)
from services.campaign_targets import CampaignTargets

TARGETS = CampaignTargets(
    threshold=1,
    slots=5,
    slots_through="this fall",
    per_day=1,
    follow_up_business_days=5,
    lookback_days_first_run=1,
    stale_after_days=1,
)
POSTAL = "123 Main St, San Francisco, CA 94110"
OPENER = "Congratulations on the Series A last week."

# Copied from spec section 8. Trailing space after "--" is the signature delimiter.
SPEC_BODY = """\
Hi {first_name},

{opening_sentence}

I'm doing {slots} Production-Readiness Audits for healthcare AI teams
{slots_through}, at no charge. I spend two days in your stack and hand you a
written report on three things:

- Compliance: where PHI flows through your LLM calls, whether your access
  boundaries, audit logging and vendor BAAs would survive an enterprise
  security review, and what an auditor would flag first.
- Performance: what each call costs, where latency and failures come from,
  and whether your model routing and fallbacks hold up.
- Delivery: whether you can change a prompt or swap a model without breaking
  production, which comes down to evals, versioning and tracing.

You get the three things standing between you and production, the fixes you
can make this month without me, and a 30-day order of operations. No PHI
leaves your environment and there's no pitch at the end.

If you want one of the {slots}, reply and I'll send the intake. If this isn't
for you, reply "no" and I won't write again.

Ben

-- 
Ben Drolet
AI Infrastructure Engineering for Healthcare
drolet.ai · linkedin.com/in/benjamindrolet
{postal_address}
"""

SPEC_FOLLOW_UP = """\
Hi {first_name}, one nudge in case this got buried. The audit is two days of my
time, a written report on what's between your AI feature and production, and
nothing to buy. Happy to send the intake if useful, and if not, no reply
needed.

Ben
"""

# Spec section 9: every message carries the postal address, the follow-up included.
SPEC_SIGNATURE = """\
-- 
Ben Drolet
AI Infrastructure Engineering for Healthcare
drolet.ai · linkedin.com/in/benjamindrolet
{postal_address}
"""


def _expected():
    return (
        SPEC_BODY.replace("{first_name}", "Sam")
        .replace("{opening_sentence}", OPENER)
        .replace("{slots_through}", "this fall")
        .replace("{slots}", "5")
        .replace("{postal_address}", POSTAL)
    )


def _render(**kw):
    args = dict(
        first_name="Sam",
        opening_sentence=OPENER,
        company_name="Acme",
        subject=None,
        targets=TARGETS,
        postal_address=POSTAL,
    )
    args.update(kw)
    return render_email(**args)


def test_body_matches_spec():
    subject, body = _render()
    assert body == _expected()
    assert "-- \n" in body


def test_default_subject_when_none():
    assert _render()[0] == "Production-readiness audit for Acme"
    assert DEFAULT_SUBJECT == "Production-readiness audit for {company}"


def test_good_subject_kept():
    assert _render(subject="Acme and PHI in your LLM calls")[0] == (
        "Acme and PHI in your LLM calls"
    )


def test_invalid_subject_falls_back():
    assert _render(subject="Free audit for Acme")[0] == DEFAULT_SUBJECT.replace("{company}", "Acme")


def test_valid_subject_rules():
    assert valid_subject("Audit for Acme", "Acme")
    assert valid_subject("audit for ACME", "Acme")
    assert not valid_subject("Free audit for Acme", "Acme")
    assert not valid_subject("Acme is carefree", "Acme")
    assert not valid_subject("Audit for Acme!", "Acme")
    assert not valid_subject("Audit for someone", "Acme")
    ok = "Acme " + "x" * 55
    assert len(ok) == 60 and valid_subject(ok, "Acme")
    assert not valid_subject(ok + "x", "Acme")


def test_placeholders_in_values_stay_verbatim():
    subject, body = _render(
        first_name="{postal_address}",
        company_name="A{slots}co",
        opening_sentence="Hi {first_name} {slots_through}",
        postal_address="{first_name} {opening_sentence} {slots}",
    )
    assert subject == "Production-readiness audit for A{slots}co"
    expected = (
        SPEC_BODY.replace("{first_name}", "\x01F")
        .replace("{opening_sentence}", "\x01O")
        .replace("{slots_through}", "this fall")
        .replace("{slots}", "5")
        .replace("{postal_address}", "\x01P")
        .replace("\x01F", "{postal_address}")
        .replace("\x01O", "Hi {first_name} {slots_through}")
        .replace("\x01P", "{first_name} {opening_sentence} {slots}")
    )
    assert body == expected


def test_nul_sequences_in_values_stay_verbatim():
    _, body = _render(opening_sentence="a \x00O b \x00F c")
    assert "a \x00O b \x00F c" in body
    assert body.startswith("Hi Sam,\n\na \x00O b \x00F c\n")


def test_follow_up_matches_spec():
    assert render_follow_up(first_name="Sam", postal_address=POSTAL) == (
        SPEC_FOLLOW_UP.replace("{first_name}", "Sam")
        + "\n"
        + SPEC_SIGNATURE.replace("{postal_address}", POSTAL)
    )


def test_follow_up_substitutes_in_one_pass():
    body = render_follow_up(first_name="{postal_address}", postal_address="{first_name}")
    assert body.startswith("Hi {postal_address}, one nudge")
    assert body.endswith("linkedin.com/in/benjamindrolet\n{first_name}\n")


def test_generic_opener():
    assert GENERIC_OPENER == "Congratulations on the round."
