# LinkedIn profile URL as a person identifier

**Status:** design. In conversation on 2026-10-13, Ben chose:
- the source of LinkedIn-only people (they become Google Contacts);
- the timing (the seed creates all of them);
- §3 (the identity and data model), which he approved as presented.

Ben asked for the spec to be written after §3. §4–§8 were drafted after that
and are being reviewed section by section; each reviewed section is marked.

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
  - In a path, the URL may be percent-encoded or raw. Starlette matches routes
    on the decoded path, so a `%2F` becomes a `/` before routing and a plain
    `{ident}` parameter 404s. The three routes declare `{ident:path}` instead
    (verified: `GET /people/linkedin.com%2Fin%2Fjane` and
    `POST /people/linkedin.com%2Fin%2Fjane/sync` both resolve).

## 4. Sync edge cases (reviewed 2026-10-13)

Once `derive()` returns `linkedin_url`, every `apply_person` write path
carries it: the linked update (`update_from_google`), linking by email
(`_link` → `set_google`, which `ensure_contact` also uses on the event path),
email-less adoption and email create (both `create_from_google`). Each of
them needs the guards below, not just the linked branch.

- **The URL is claimed by another row.** Two Google contacts carry the same
  profile URL. Writing it to the second one would violate the UNIQUE
  constraint and abort the entire nightly sync.
  - One check runs right after `derive()`, before any branch: if
    `repo/people.py::linkedin_owner(conn, url)` returns an id other than the
    row being written (the linked row's own id, or none for a new row), the
    derived `linkedin_url` is replaced with `None`. The owner check must
    exclude the row itself, since a linked row normally already holds its
    own URL. A check written like `email_owner` ("anyone owns it") would
    strip every LinkedIn-only row's URL on its next sync.
  - The contact is then written as usual without the URL. If that leaves it
    with no identifier, it is not adopted, and `run_sync` counts it under a
    new `linkedin_conflict` kind (a count only, never names).
- **The only identifier disappears.** The existing linked-branch guard
  (`email is None and not phone_numbers`) is rewritten as: no email, no
  derived phone, and no `linkedin_url` left after the conflict check. If it
  holds, the update is skipped and the row keeps what it has, as a phone-only
  row keeps its last phone today. Extending the old condition instead would
  wrongly skip a phone-only row whose contact swapped its phone for a URL.
- **PATCH keeps an identifier too.** `PATCH contact.urls` could otherwise
  delete a LinkedIn-only person's URL in Google. `sync_one` would then skip
  the refresh without saying so, and the row would no longer match Google.
  `contact_fields.check_phone_removal` becomes
  `check_identifier_kept(row, fields)`. It covers `phoneNumbers` and `urls`
  and raises `IdentifierRuleError` (409) when the submission would leave the
  person with no email, no usable phone and no normalizable profile URL. It
  also stops wrongly refusing an empty `phoneNumbers` from a person who still
  has a LinkedIn URL.
- **Deleted contacts are unchanged.** A `google_deleted_at` row keeps its
  URL, so a re-created contact with the same URL conflicts, just as a
  re-created contact with a tombstoned email is left alone today.

## 5. The seed creates LinkedIn-only contacts (reviewed 2026-10-13)

`services/outreach_seed.py` stays pure. It already reports warm LinkedIn rows
with `person_id IS NULL` as `unlinked`. It gains one new piece of output: for
each unlinked LinkedIn row, exactly one outcome, checked in this order.

- **`link <id>`.** A `people` row already holds this normalized URL in
  `people.linkedin_url`. It got there through rollout step 3's adoption, a
  possible duplicate Ben resolved by adding the URL (§8 step 5), or an
  earlier seed run that `import_linkedin` hasn't re-matched yet. Google is
  not touched.
- **`in Google, not synced`.** No `people` row holds the URL, but a Google
  contact does (see the Google index below). Listed, never created: the
  nightly sync adopts it, and the next run links it.
- **`possible duplicate of <ids>`.** A `people` row, or a Google contact with
  no row, whose name shares the normalized last name and first initial, for
  example "Jon Smith" for "Jonathan Smith" (placeholder names; the repo is
  public). A Google-only match carries no id and is shown as "in Google, no
  row". These are listed, never created.
- **`create`.** None of the above.

**The Google index.** The People API's `searchContacts` doesn't index `urls`,
but the repo already reads the whole contact list for phone duplicates
(`clients/google_contacts.py::list_phone_index`, one page per 1,000
contacts). A matching `list_url_index()` does the same for profile URLs
(normalized with `normalize_profile_url`) and returns each contact's name
alongside. The script fetches it once per run and passes the URL and name
indexes to `plan()`. The name index is what makes D4 hold on the Google side
too: the 87 Google contacts with no identifier at all (measured 2026-09-28)
have no `people` row, so a name check against `people` alone can't see them.

**Inputs.** `plan()` gains `people_rows` (`id`, `display_name`,
`linkedin_url`) and the two Google indexes. `repo/outreach.py::linkedin_seed_rows`
also selects `first_name` and `last_name`, which `linkedin_connections`
already stores.

`scripts/outreach_seed.py` then works through the `link` and `create`
entries.

1. **`link`** sets `linkedin_connections.person_id` with `match_method = 'url'`
   and enters the person at `candidate` (`outreach.ensure_contact`,
   `source = 'seed'`).
2. **`create`** calls `services/person_create.py::create(conn, contact=...)`.
   That is the same path as `POST /people`, so validation, the duplicate
   refusal and `apply_person` are reused. The contact holds:
   - `names: [{givenName, familyName}]` from the export;
   - `organizations: [{name: company, title: position}]`;
   - `urls: [{value: "https://www.linkedin.com/in/<slug>", type: "profile"}]`.

   The call adds a `from-linkedin` label, so Ben can find or clean these in
   Google Contacts. It then links and enters the person exactly as `link`
   does.
3. `person_create.create` accepts a LinkedIn URL as the identifier
   (`contact_fields.identifiers` returns it). It refuses a duplicate found by
   `people.linkedin_url` (409 with the existing id), and a Google-only match
   from `list_url_index()` (409 with no candidates), as it does for phones.
4. The script commits after each person, so `run()` stops being one
   `with get_conn()` transaction.
   - A Google or quota failure on one person is reported and counted. The
     run continues.
   - Re-running is safe. A created person now has a `people.linkedin_url`,
     so a second run classifies them as `link`. If the Google create
     succeeded but the DB commit didn't, the Google index catches the
     orphan as `in Google, not synced` instead of creating a second contact.
5. Google calls retry on HTTP 429 with backoff (`execute(num_retries=…)`).
   Nothing in `clients/google_contacts.py` retries today, so this is new.
   Creation is sequential.

`--dry-run` prints four counts: link, create, possible duplicate, and in
Google not synced. It then lists every entry with its candidate ids, and
writes nothing. Unlinked iMessage and WhatsApp rows are still listed
separately, as today.

The CLI needs no new channel: `outreach mark <person> briefed --channel
linkedin` and `outreach nudge … --channel linkedin` already exist. A
LinkedIn-only person has no email, so their briefs are never email-proven.
They are recorded by hand after Ben confirms, as the skill already says.

## 6. Incidental fix (reviewed 2026-10-13)

`outreach today` pads the channel column to 24 characters with no separator,
so anything longer runs into `last`. `services/outreach.py::_channels` reports
four channels, joined in the order email, imessage, linkedin, whatsapp: all
four make "email · imessage · linkedin · whatsapp", 38 characters. Every
seeded LinkedIn person carries `linkedin`, so long strings become common.

The fix is in `services/outreach_today.py::_brief_line`. It pads to
`_CHANNELS_WIDTH`, the width of all four channels joined (computed from the
channel names, not hardcoded), and adds a separating space:
`f"{channels:<{_CHANNELS_WIDTH}} last {last}"`. `last` then starts in the
same column whatever mix of channels a person has. A full line is about 85
characters.

## 7. Testing (reviewed 2026-10-13)

Unit tests use the existing fakes. The real-Postgres tests run only with
`TEST_DATABASE_URL` set (a throwaway `postgres:16` container locally; CI sets
it).

**§3: identity and data model**
- `tests/test_linkedin_export.py`:
  - `normalize_profile_url`: scheme, `www`, query string, fragment, trailing
    slash and case are stripped; a company URL (`/company/…`) and a bare
    `linkedin.com` give `None`. This is a behaviour change: today a company
    URL comes back unchanged. Production has none outside `/in/` (measured
    2026-10-13: 0 of 1,885 connection URLs, 0 message sender URLs), so no
    stored key changes;
  - parsing a messages CSV whose sender is a non-profile URL drops it, as a
    blank one is dropped today;
  - `match_people`: the URL step comes first, wins over email and name, and
    sets `match_method = 'url'`.
- `tests/test_import_linkedin.py`: `names_for_matching` returns
  `linkedin_url`, and the import links by it.
- `tests/test_contact_fields.py`: `derive` returns `linkedin_url` from `urls`
  (the first one that normalizes wins; a non-profile URL is ignored;
  `None` when there is none); `identifiers` returns it.
- `tests/test_identity.py`: a profile URL, with or without scheme, classifies
  as `linkedin`; `linkedin.com/in/12345` is `linkedin`, not an id or a phone;
  an email containing "linkedin" is still an email.
- `tests/test_person_lookup.py`: resolves a person by URL.
- `tests/test_api.py`: GET, PATCH and `POST …/sync` resolve a person by a
  `%2F`-encoded URL and by a raw one (`{ident:path}`).

**§4: sync edge cases**
- `tests/test_google_contacts_sync.py`:
  - adopting a LinkedIn-only contact;
  - a URL claimed by another row is dropped on each of the four write paths
    (linked update, `_link`, email-less adoption, email create); a contact
    left with no identifier is counted `linkedin_conflict`;
  - a linked row keeps its own URL across repeated syncs (the owner check
    excludes the row itself);
  - a row's last identifier is kept when Google loses it;
  - a phone-only row whose contact swaps its phone for a URL is updated,
    not skipped;
  - a LinkedIn-only row that gains an email is promoted.
- `tests/test_repo_people.py`: `linkedin_owner` returns the holder's id or
  `None`.
- `tests/test_contact_fields.py`: `check_identifier_kept` refuses a `urls`
  submission that removes a LinkedIn-only person's last URL, allows an empty
  `phoneNumbers` while a URL remains, and keeps today's phone-only behaviour.
- `tests/test_person_edit.py`: that refusal is a 409 and writes nothing to
  Google.

**§5: the seed**
- `tests/test_google_contacts.py`: `list_url_index` normalizes URLs, keeps
  names, ignores non-profile URLs, and pages; Google calls pass
  `num_retries`.
- `tests/test_person_create.py`: a URL-only contact is accepted; a duplicate
  `people.linkedin_url` is a 409 naming the existing id; a Google-only URL
  match is a 409 with no candidates.
- `tests/test_outreach_seed.py`, `plan()`:
  - each of the four outcomes, and their precedence (`link` before
    `in Google, not synced` before `possible duplicate` before `create`);
  - the last-name-plus-first-initial rule against `people` rows and against
    Google-only names.
- `tests/test_outreach_seed.py`, the script with scripted fakes:
  - `link` sets `person_id` and enters at `candidate` without calling Google;
  - `create` commits per person; one failure doesn't stop the run;
  - a re-run links instead of creating;
  - an orphaned Google contact (create succeeded, commit failed) is reported
    as `in Google, not synced`, not created again;
  - the dry run prints the four counts and writes nothing.

**§6: the `today` column**
- `tests/test_outreach_today.py`: with all four channels, `last` is preceded
  by a space and starts in the same column as on a one-channel line.

**Real Postgres** (`tests/test_schema.py`, `tests/test_outreach_db.py`)
- the constraint accepts a row with only `linkedin_url` and rejects a row with
  none of the three;
- the migration runs twice, and resumes after a partial apply;
- the UNIQUE constraint on `linkedin_url` holds, and several NULLs coexist;
- `create_from_google`, `update_from_google` and `set_google` round-trip
  `linkedin_url`.

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
- Merging the existing duplicate `people` rows (measured 2026-10-13: one
  person with two rows, another with three). Those use
  `scripts/merge_duplicate_contacts.py`, as today.
