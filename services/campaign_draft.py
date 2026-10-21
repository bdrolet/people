"""Rendering of the funded-founder campaign email and follow-up (spec §8). Pure text: no
I/O. Placeholders are substituted with str.replace, not str.format, so braces in a
company name cannot break rendering."""

from services.campaign_targets import CampaignTargets

DEFAULT_SUBJECT = "Production-readiness audit for {company}"
GENERIC_OPENER = "Congratulations on the round."
MAX_SUBJECT_LENGTH = 60

BODY_TEMPLATE = """\
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
"""

SIGNATURE = (
    "-- \n"
    "Ben Drolet\n"
    "AI Infrastructure Engineering for Healthcare\n"
    "drolet.ai · linkedin.com/in/benjamindrolet\n"
    "{postal_address}\n"
)

FOLLOW_UP_TEMPLATE = """\
Hi {first_name}, one nudge in case this got buried. The audit is two days of my
time, a written report on what's between your AI feature and production, and
nothing to buy. Happy to send the intake if useful, and if not, no reply
needed.

Ben
"""


def valid_subject(subject: str, company_name: str) -> bool:
    return (
        len(subject) <= MAX_SUBJECT_LENGTH
        and "!" not in subject
        and "free" not in subject.lower()
        and company_name.lower() in subject.lower()
    )


def render_email(
    *,
    first_name: str,
    opening_sentence: str,
    company_name: str,
    subject: str | None,
    targets: CampaignTargets,
    postal_address: str,
) -> tuple[str, str]:
    if subject is None or not valid_subject(subject, company_name):
        subject = DEFAULT_SUBJECT.replace("{company}", company_name)
    body = BODY_TEMPLATE + "\n" + SIGNATURE.replace("{postal_address}", postal_address)
    # Substitute caller-supplied text last-to-first so a value containing a placeholder
    # (e.g. an opener with "{slots}") is never re-expanded.
    body = body.replace("{slots_through}", "\x00T").replace("{slots}", "\x00S")
    body = body.replace("{first_name}", "\x00F").replace("{opening_sentence}", "\x00O")
    for token, text in (
        ("\x00T", targets.slots_through),
        ("\x00S", str(targets.slots)),
        ("\x00F", first_name),
        ("\x00O", opening_sentence),
    ):
        body = body.replace(token, text)
    return subject, body


def render_follow_up(*, first_name: str) -> str:
    return FOLLOW_UP_TEMPLATE.replace("{first_name}", first_name)
