"""Rendering of the funded-founder campaign email and follow-up (2026-10-29 launch spec §5,
which supersedes the 2026-10-21 campaign spec §8). Pure text: no
I/O. Placeholders are substituted with str.replace, not str.format, so braces in a
company name cannot break rendering."""

import re

from services.campaign_targets import CampaignTargets

DEFAULT_SUBJECT = "Production-readiness review for {company}"
GENERIC_OPENER = "Congratulations on the round."
MAX_SUBJECT_LENGTH = 60
_PLACEHOLDER = re.compile(r"\{(first_name|opening_sentence|slots_through|slots|postal_address)\}")

BODY_TEMPLATE = """\
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
"""

SIGNATURE = (
    "-- \n"
    "Ben Drolet\n"
    "AI Infrastructure Engineering for Healthcare\n"
    "drolet.ai · linkedin.com/in/benjamindrolet\n"
    "{postal_address}\n"
)

FOLLOW_UP_TEMPLATE = """\
Hi {first_name}, one nudge in case this got buried. The review is one week of
my time, read-only, no PHI, and a written report on what a health system's
security review will find, with nothing to buy. Happy to find 45 minutes if
useful, and if not, no reply needed.

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
    values = {
        "first_name": first_name,
        "opening_sentence": opening_sentence,
        "slots_through": targets.slots_through,
        "slots": str(targets.slots),
        "postal_address": postal_address,
    }
    # One pass: no substituted value is ever re-scanned for placeholders.
    body = _PLACEHOLDER.sub(lambda m: values[m.group(1)], BODY_TEMPLATE + "\n" + SIGNATURE)
    return subject, body


def render_follow_up(*, first_name: str, postal_address: str) -> str:
    """The nudge, then the signature: spec §9 wants the postal address in every message."""
    values = {"first_name": first_name, "postal_address": postal_address}
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], FOLLOW_UP_TEMPLATE + "\n" + SIGNATURE)
