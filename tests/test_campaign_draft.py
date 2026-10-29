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

# Copied from spec section 5 (2026-10-29 launch spec). Trailing space after "--" is the signature delimiter.
SPEC_BODY = """\
Hi {first_name},

{opening_sentence}

Somewhere between the pilot and the contract, the health system's security
team sends the questionnaire and asks for a BAA, and "where does the data
live and who has access" has to be answered in writing. For most small teams
the honest answer includes a logging tool, a dev tool or a model provider
setting nobody put under a BAA, and finding that out mid-review costs a
quarter, or the deal.

I'm doing about {slots} Production-Readiness Reviews for healthcare startups
with an LLM feature {slots_through}, one per week, at no charge. One week,
read-only, no PHI. You get a written report you can hand a buyer: where PHI
actually flows, which hops have a BAA and which don't, which of the fifteen
questions health systems now ask about AI you can truthfully answer today,
what breaks at the next prompt change or model deprecation, and the fix list
in order, each item marked weekend, sprint or rebuild.

I built Homeward Health's AI platform from the ground up, PHI boundaries and
audit trails included, and that infrastructure has been through health system
security reviews, BAA negotiations and SOC 2.

I don't sell a platform, a certificate or a remediation product. I make my
living on paid engineering work; some reviews lead to that, most don't, and
the report is yours either way. Nothing connects to your repo: you show me
first, and any access later is a read-only login you can revoke.

How it works, the report's sections and who I am: drolet.ai/review

If you'd like one, reply and we'll find 45 minutes. If this isn't for you,
reply "no" and I won't write again.

Ben

-- 
Ben Drolet
AI Infrastructure Engineering for Healthcare
drolet.ai · linkedin.com/in/benjamindrolet
{postal_address}
"""

SPEC_FOLLOW_UP = """\
Hi {first_name}, one nudge in case this got buried. The review is one week of
my time, read-only, no PHI, and a written report on what a health system's
security review will find, with nothing to buy. Happy to find 45 minutes if
useful, and if not, no reply needed.

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
    assert _render()[0] == "Production-readiness review for Acme"
    assert DEFAULT_SUBJECT == "Production-readiness review for {company}"


def test_good_subject_kept():
    assert _render(subject="Acme and PHI in your LLM calls")[0] == (
        "Acme and PHI in your LLM calls"
    )


def test_invalid_subject_falls_back():
    assert _render(subject="Free review for Acme")[0] == DEFAULT_SUBJECT.replace("{company}", "Acme")


def test_valid_subject_rules():
    assert valid_subject("Review for Acme", "Acme")
    assert valid_subject("review for ACME", "Acme")
    assert not valid_subject("Free review for Acme", "Acme")
    assert not valid_subject("Acme is carefree", "Acme")
    assert not valid_subject("Review for Acme!", "Acme")
    assert not valid_subject("Review for someone", "Acme")
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
    assert subject == "Production-readiness review for A{slots}co"
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


def test_body_has_no_countdown_or_audit():
    _, body = _render()
    low = body.lower().replace("audit trails", "")
    assert "audit" not in low
    assert "if you want one of the" not in low
    assert "drolet.ai/review" in body
