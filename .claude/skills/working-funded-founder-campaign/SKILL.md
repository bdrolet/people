---
name: working-funded-founder-campaign
description: >
  Use when running Ben's funded-founder cold-email campaign — "run the
  funded-founder campaign", "find this week's funded digital health
  companies", "campaign review", "campaign report", "who bounced", "work the
  campaign", "record that X bounced / opted out / booked". Runs the campaign
  CLI in ~/src/people and supplies the research and writing judgment: which
  rounds count, how a company scores, who the buyer is, the one opening
  sentence. Not for referral outreach (working-outreach-queue).
metadata:
  depends-on: "anthropic-skills:chrome-browser, searching-people"
---

# Working the funded-founder campaign

Each week: find freshly funded US digital health companies, score the fit,
find the technical buyer, draft one short email per person, and let Ben
approve and send from a review page. Every count and status lives in the
`campaign_*` tables; this skill runs the CLI and supplies the judgment.
Design: `~/src/consulting/docs/superpowers/specs/2026-10-21-funded-founder-campaign-design.md`.

## Weekly checklist

- Run `weekly` on a weekday morning (it sends due follow-ups, then opens the review page).
  Not until inbox PR #68 is deployed (see Rules); until then run `review`.
- Never run `send` twice on the same day while `needs_check` is non-empty.
- Read `campaign report` afterwards, including "Needs attention".

## 1. Setup

```bash
cd ~/src/people
C=".venv/bin/python scripts/campaign.py"
```

If a command says `CAMPAIGN_TARGETS` is missing, run `scripts/fetch-env.sh`.
Usage errors (bad flags) exit 1; read the message and fix the call.

## 2. Find the rounds

```bash
$C run start                      # first run: add --lookback-days 30
```

It prints `run N: <from> to <to>`; that is the window. Search, in order:

1. Rock Health's weekly funding roundup
2. Fierce Healthcare funding coverage
3. MobiHealthNews
4. Axios Pro Rata
5. TechCrunch health
6. Web search: `"digital health" raises ("Series A" OR "Series B" OR seed)` within the window

For each company capture: domain, name, round (`seed`, `a`, `b`, `other`),
announcement date, source URL, a one-line product summary, optional amount,
and HQ country. Copy 300 to 1,500 characters of the announcement **verbatim**
into `.campaign/excerpts/<domain>.txt`; the fact check later compares the
opener against this file, so paraphrase defeats it. Then:

```bash
mkdir -p .campaign/excerpts
$C company add --domain acme.health --name "Acme Health" --round a \
  --announced-on 2026-10-14 --source-url https://... \
  --product "AI intake for behavioral health clinics" \
  --excerpt-file .campaign/excerpts/acme.health.txt \
  [--amount 12M] [--country US]
```

It prints `added N` or `exists N`. Non-US companies are still added, with
`--country`, so the dedupe remembers them (they land at `below_bar` with note "non-US" and are never scored or drafted). Keep the
source list for the final summary to Ben.

## 3. Score

Read the company's site, the announcement, its careers page and LinkedIn.
Score 0 to 10 with this rubric:

| Signal | Points | Evidence looked for |
|---|---|---|
| Stage Series A or B (seed scores 1, later rounds 0) | 2 | the announcement |
| LLM or "AI" feature live or in pilot | 2 | product pages, announcement, engineering blog |
| PHI in the product (clinical, patient-facing, payer data) | 2 | product description, HIPAA/BAA mentions, security page |
| Sells to health systems, payers or employers (B2B) | 1 | customers page, announcement |
| Engineering team 10 to 60 | 1 | LinkedIn headcount, careers page |
| No AI platform / ML infra lead on the team, or an open req for one | 1 | LinkedIn, careers page |
| Behavioral health or Ben's adjacent domains | 1 | product description |

```bash
$C company score <id> --score 7 --notes "A +2, LLM pilot +2, PHI +2, B2B +1; no ML lead"
```

One short note per signal that scored. The threshold comes from
`campaign_targets.toml`; the output is the new status (`active` or
`below_bar`; `warm` is set by the warm-contact guard). Ben can override with
`$C company promote <id>`. `$C company list [--status active]` shows state.

## 4. Find the buyer

For each company added and scored `active` in THIS run (the ids `company add`
printed this run that `$C company list --status active` shows; never an `active`
company from an earlier run): CTO, VP Engineering, Head of Engineering, in that
order; the CEO at seed or when none of those is listed. At most two people
per company (the technical buyer and, at seed or when the buyer cannot be
verified, the CEO).

The Apollo API is not available on the free plan (verified 2026-10-22: HTTP 403),
so find the buyer on LinkedIn through Chrome (CTO / VP Engineering / Head of
Engineering, CEO at seed), optionally cross-checking in the Apollo web UI by
hand. Call `contact add` with `--linkedin` and no `--email`; `contact verify`
then finds and verifies the address through Hunter.

If a paid Apollo plan is ever added, this finds people by title:

```bash
.venv/bin/python -c "from dotenv import load_dotenv; load_dotenv(); from clients import apollo; print(apollo.people_search('acme.health', ['CTO','VP Engineering','Head of Engineering']))"
```

Then add each person with what you have:

```bash
$C contact add --company <id> --name "Jane Doe" --title CTO \
  [--linkedin https://linkedin.com/in/...] [--email jane@acme.health]
```

Output is `candidate N`, `skipped_warm N`, `existing N` or `opted_out N`.
`skipped_warm` means the guard found Ben already knows them (or the company is
already `warm`); leave it. If the
people DB (`searching-people`) shows a real conversation the guard missed,
stop and tell Ben.

## 5. Verify

```bash
$C contact verify --all
```

One line per contact: `id email verification status`. Only `valid` can be
drafted; the rest are parked as `unverified` and shown on the review page. A
line `skipped_warm N (warm)` or `skipped_warm N (bounced address)` means the
email found belongs to someone Ben knows or an address that hard-bounced; the
contact is parked, leave it. A contact Hunter cannot look up (a 4xx for its
domain or name) is parked `unverified` with a `verify failed: ...` note, prints
`id email error reason`, and the rest continue.

## 6. Draft

For each verified contact write:

- **Opener**: one sentence that names the round and the product in the
  announcement's own words. Nothing the excerpt does not say.
- **Subject**: under 60 characters, names the company.

```bash
$C contact draft <id> --opener "Congrats on Acme's Series A to bring AI intake to behavioral health clinics." \
  --subject "Acme Health and AI in clinical workflows"
```

Exit 2 means the fact check failed; stderr lists the unsupported claims (the
subject is checked with the opener when you pass `--subject`). Rewrite the
opener (and subject) once, using only what the claims left out, and retry. On a
second exit 2, use the generic opener:

```bash
$C contact draft <id> --generic
```

Exit 3 means the fact check could not run at all (`fact check could not run:
...` on stderr, usually the `claude` CLI failing). Do not use `--generic`: stop
and tell Ben, with the message.

Exit 1 with `cannot be drafted: <reason>` (`company warm`, `warm`, `bounced
address`, ...) means the contact must not be emailed; leave it.

## 7. Review and send

```bash
$C weekly        # followups, then the review page
```

`followups` runs the same preflight as `send` and shares its daily cap: it
prints `sent N (fallback M, skipped K, failed F)`, plus `remaining R` when the
cap ran out (run `followups` again tomorrow); if F > 0 it points at `campaign
report` (Needs attention lists contacts to check by hand). `review` then serves the page and opens it. Tell Ben the URL is open
and wait for him to submit; submitting approves and runs `send`, which prints:

- `sent N, remaining M`. If M > 0, tell Ben to run `campaign send` tomorrow morning.
- `not sent: <id> (<reason>)`: the gate stopped those contacts at send time
  (opted out, warm, bounced address). Relay them; they are never sent.
- a halt line (`halted: more than two bounces; run campaign resume`). Stop and tell Ben.
- a `needs_check` warning: Ben must look in Outlook Sent Items/Drafts for those
  contact ids. Do not rerun `send` the same day while it is non-empty.

To review without followups: `$C review`. To send without the page: `$C send`.
End with `$C report` and relay the counts and any "Needs attention" contacts.

## 8. Rules

- Never invent a fact about a company; the opener comes from the excerpt.
- Never add a contact Ben knows. The guard catches most; if the people DB
  shows a conversation, stop and tell Ben.
- Never run `send` or `resume` without Ben saying so after a halt. `resume`
  bypasses the bounce halt for one batch and only runs when Ben says so.
- Hand commands are recorded only when Ben confirms:
  `$C bounced <id|email>`, `$C optout <id|email> [--note "..."]` (stops the
  whole company), `$C booked <id|email>`.
- Never send twice the same day while `needs_check` is non-empty.
- Do not run `followups` or `weekly` until inbox PR #68 is deployed: before it,
  a follow-up reply goes to Ben instead of the contact and is still recorded
  as sent. Until then use `$C review` (or `$C send`) only.
- `contact draft` exit 3 (the fact check could not run): stop and tell Ben;
  never fall back to `--generic` for it.
