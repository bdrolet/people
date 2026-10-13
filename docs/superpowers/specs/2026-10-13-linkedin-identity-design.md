# LinkedIn profile URL as a person identifier

**Status:** design. In conversation on 2026-10-13, Ben chose:
- the source of LinkedIn-only people (they become Google Contacts);
- the timing (the seed creates all of them);
- §3 (the identity and data model), which he approved as presented.

Ben asked for the spec to be written after §3. §4–§8 are drafted and not yet
reviewed, so confirm them at spec review.

**Builds on:**
- `2026-09-24-person-identity-design.md`: id/email/phone identity and the
  `people_has_an_identifier` constraint;
- `2026-09-28-adopt-emailless-contacts-design.md`: adoption;
- `2026-09-28-post-people-design.md`: `POST /people` and promotion;
- `2026-10-13-outreach-working-queue-design.md`: the outreach seed and queue.

## 1. Problem

The LinkedIn snapshot holds 1,885 connections, 181 of them with two-way
message threads. Only 5 are linked to a `people` row, because the export
almost never carries an email address. The outreach seed found 96 warm
LinkedIn contacts: two-way, messaged in the last two years, not recruiters.
It placed none of them in the pipeline, because a pipeline record needs a
`people` row, and a `people` row needs an email or a phone number.

Measured on 2026-10-13, the 96 include healthcare founders and CTOs,
life-sciences engineers, investors and AI-platform leaders. They are the
strongest referrers in Ben's network.

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | A LinkedIn-only person becomes a real Google Contact, with name, organization and the LinkedIn URL in `urls`. Their `people` row derives from it. | Google Contacts stays the single source of truth for who a person is. A rebuild from Google keeps them. Labels and notes work as they do for everyone. |
| D2 | The LinkedIn profile URL becomes a third identifier: a person needs an email, a phone, or a LinkedIn URL. | This revises the rule Ben stated in the identity design ("email or phone, never neither"). A LinkedIn profile is as specific as either. |
| D3 | The outreach seed creates the Google Contact for every warm, non-recruiter LinkedIn connection that has no `people` row. It then enters each one at `candidate` like any other seed result. | Ben's choice. One uniform path, and every warm contact is visible to reassessment. |
| D4 | The seed never creates a contact that might duplicate an existing person. It lists the possible duplicate instead. | A wrong second record is worse than a missing one, which is the same rule `match_people` follows. |
| D5 | LinkedIn-only people are never mirrored to HubSpot. | HubSpot is keyed by email, as for phone-only people. |

## 3. Identity and data model (approved)

- **Column.** `people.linkedin_url TEXT UNIQUE`. It is NULL for most rows.
- **Normal form.** `linkedin.com/in/<slug>`: lowercase, with no scheme, no
  `www.`, no query string and no trailing slash.
  - One normalizer is shared by the export parser, Google derivation and
    identity classification: `services/linkedin_export.py::normalize_profile_url`.
  - A URL that does not match `linkedin.com/in/<slug>` (a company page, for
    example) normalizes to `None` and identifies no one.
- **Constraint.** `people_has_an_identifier` becomes
  `email IS NOT NULL OR cardinality(phone_numbers) > 0 OR linkedin_url IS NOT NULL`.
  It is replaced idempotently in `repo/schema.sql`, using the same `DO $$`
  pattern as the outreach stage CHECK, and with no `%` characters.
- **Derivation.** `contact_fields.derive()` returns `linkedin_url`: the first
  `urls[].value` that normalizes, or `None`. `urls` is already on the
  allowlist and in `PERSON_FIELDS`, so no read-mask change is needed and no
  full resync is forced.
- **Adoption.** `apply_person` adopts a Google contact with no email and no
  usable phone when it has a LinkedIn URL. It calls
  `create_from_google(conn, None, ..., linkedin_url=...)`. A contact with none
  of the three identifiers is still counted as `skipped`.
- **Promotion is unchanged.** When a LinkedIn-only person's contact later gains
  an email, the existing promotion step writes it onto the same row.
- **Connections link by URL.** `linkedin_connections.person_id` is set by an
  exact match on the normalized URL against `people.linkedin_url`. This is a
  new first step in `match_people`, ahead of email and name, with
  `match_method = 'url'`.
- **Addressing.** `services/identity.py::classify` checks for an email first.
  Next, anything that normalizes as a LinkedIn profile URL is classified as
  `("linkedin", url)`. Then come phone and id, as today.
  - `GET/PATCH /people/{ident}`, `POST /people/{ident}/sync` and every
    `outreach <person>` argument accept the URL.
  - In a path, the URL is percent-encoded.

## 4. Sync edge cases (drafted)

`apply_person`'s already-linked branch writes `linkedin_url` like
`phone_numbers`, with two guards that mirror the email-promotion guards.

- **The URL is claimed by another row.** Two Google contacts carry the same
  profile URL. Writing it to the second one would violate the UNIQUE
  constraint and abort the entire nightly sync.
  - The second contact keeps `linkedin_url = NULL`.
  - If that leaves it with no identifier, it is not adopted, and `run_sync`
    counts it under a new `linkedin_conflict` kind (a count only, never
    names).
  - `repo/people.py::linkedin_owner(conn, url)` makes the check, as
    `email_owner` does for email.
- **The only identifier disappears.** If someone deletes the LinkedIn URL in
  Google from a row whose only identifier it was, the update is skipped and
  the row keeps its URL. This is the same guard that protects a phone-only
  row from losing its last phone.

## 5. The seed creates LinkedIn-only contacts (drafted)

`services/outreach_seed.py` stays pure. It already reports warm LinkedIn rows
with `person_id IS NULL` as `unlinked`. It gains one new piece of output: for
each unlinked LinkedIn row, a classification.

- **`create`.** Nothing in `people` could be this person.
- **`possible duplicate of <ids>`.** A `people` row whose `display_name`
  shares the normalized last name and first initial. Examples are
  "Anshu Sinha" for "Anshuman Sinha", and the two rows for BiL Castine.
  These are listed, never created.

`scripts/outreach_seed.py` then works through the `create` entries.

1. It calls `services/person_create.py::create(conn, contact=...)`. That is
   the same path as `POST /people`, so validation, the duplicate refusal and
   `apply_person` are reused. The contact holds:
   - `names: [{givenName, familyName}]` from the export;
   - `organizations: [{name: company, title: position}]`;
   - `urls: [{value: "https://www.linkedin.com/in/<slug>", type: "profile"}]`.

   The call adds a `from-linkedin` label, so Ben can find or clean these in
   Google Contacts.
2. `person_create.create` accepts a LinkedIn URL as the identifier, extending
   `contact_fields.identifiers` to return it. It refuses a duplicate found by
   `people.linkedin_url`.
   - There is no Google-side URL search, because the People API's
     `searchContacts` doesn't index `urls`.
   - The safeguard is rollout step 3 (§8): a full sync before the first
     seeded run adopts every existing contact that carries a LinkedIn URL.
3. It sets `linkedin_connections.person_id` for that profile URL, with
   `match_method = 'url'`.
4. It enters the person at `candidate` (`outreach.ensure_contact`,
   `source = 'seed'`).
5. It commits after each person.
   - A Google or quota failure on one person is reported and counted. The run
     continues.
   - Re-running is safe: a created person now has a `people.linkedin_url`, so
     a second run links instead of creating.

`--dry-run` prints three counts: created, possible duplicates and skipped. It
then lists every `create` and every possible duplicate with its candidate ids,
and writes nothing. Creation is sequential, retrying on HTTP 429.

The CLI needs no new channel: `outreach mark <person> briefed --channel
linkedin` and `outreach nudge … --channel linkedin` already exist. A
LinkedIn-only person has no email, so their briefs are never email-proven.
They are recorded by hand after Ben confirms, as the skill already says.

## 6. Incidental fix

`outreach today` pads the channel column to 24 characters. "email · imessage ·
whatsapp" is 26, so it runs into `last`. The fix adds a separating space in
`services/outreach_today.py::_brief_line`.

## 7. Testing (drafted)

- `tests/test_linkedin_export.py`:
  - `normalize_profile_url`: scheme, `www`, query string, trailing slash,
    case, and a company URL giving `None`;
  - `match_people` gains a URL step that takes precedence over name.
- `tests/test_contact_fields.py`:
  - `derive` returns `linkedin_url` from `urls`: the first valid one wins, and
    a non-profile URL is ignored;
  - `identifiers` includes it.
- `tests/test_identity.py`: a profile URL classifies as `linkedin`; an email
  containing "linkedin" is still classified as an email.
- `tests/test_google_contacts_sync.py`:
  - adopting a LinkedIn-only contact;
  - a claimed URL counted as `linkedin_conflict` and not written;
  - the last identifier kept;
  - a later email promoted.
- `tests/test_person_create.py`:
  - a URL-only contact is accepted;
  - a duplicate URL gets a 409 naming the existing id.
- `tests/test_outreach_seed.py`: classification into create and possible
  duplicate, including the last name plus first initial rule.
- Script test (`tests/test_outreach_seed.py`, scripted fakes):
  - per-person commit;
  - one failure doesn't stop the run;
  - a re-run links instead of creating;
  - the dry run writes nothing.
- Real-Postgres tests (`tests/test_schema.py`, `tests/test_outreach_db.py`):
  - the constraint accepts a row with only `linkedin_url` and rejects a row
    with none of the three;
  - the migration runs twice;
  - the UNIQUE constraint on `linkedin_url` holds.
- `tests/test_outreach_today.py`: the channel column is separated from `last`
  when all three channels are present.

## 8. Rollout (drafted)

1. Run `scripts/migrate_db.py` against production before merging.
2. Merge. The functions and people-api deploy, and the nightly sync starts
   deriving `linkedin_url`.
3. Run `scripts/clear_sync_token.py`, then trigger `people-sync` once. That
   full pass adopts existing Google contacts that carry a LinkedIn URL, which
   step 5's duplicate check depends on.
4. Run `.venv/bin/python scripts/import_linkedin.py <latest export>`, so
   connections link by URL.
5. Run `.venv/bin/python scripts/outreach_seed.py --dry-run`. Ben reviews the
   creates and the possible duplicates.
   - To merge a possible duplicate, add the LinkedIn URL to the existing
     Google contact with `editing-person`, which updates its `urls`, then
     re-run the dry run.
6. Run `scripts/outreach_seed.py`, then reassess the queue with the
   `working-outreach-queue` skill.

## 9. Out of scope

- Proving LinkedIn briefs, nudges or replies from the message snapshot. That
  is phase 2 of the outreach design; for now LinkedIn events are recorded by
  hand.
- Fetching emails for LinkedIn contacts (PDL or similar enrichment).
- Creating Google Contacts for LinkedIn connections the seed doesn't consider
  warm.
- Merging the existing duplicate `people` rows (BiL Castine 128/532; Renee
  Drolet 152/170/329). Those use `scripts/merge_duplicate_contacts.py`, as
  today.
