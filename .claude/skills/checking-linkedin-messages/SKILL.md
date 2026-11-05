---
name: checking-linkedin-messages
description: >
  Use when the user asks whether anyone messaged them on LinkedIn — "did anyone
  reply on LinkedIn", "check my LinkedIn messages", "any LinkedIn replies to my
  outreach", "who messaged me on LinkedIn", "what did X say on LinkedIn" — or when
  outreach briefs went out by LinkedIn and there is no fresh LinkedIn data export
  to import. Reads the inbox and threads through the LinkedIn MCP server, with
  notification emails and Chrome as fallbacks.
metadata:
  depends-on: "searching-inbox-emails, fetching-inbox-email, working-outreach-queue, importing-linkedin, anthropic-skills:chrome-browser"
---

# Checking LinkedIn messages

The people DB only sees LinkedIn messages from a data export
(**importing-linkedin**), which goes stale between exports. For anything newer,
read LinkedIn itself through the **`linkedin` MCP server** (`.mcp.json` in
`~/src/people`, package `mcp-server-linkedin`). It drives a headless browser
signed in as Ben, with its own saved session in `~/.linkedin-mcp/`.

Tools (all prefixed `mcp__linkedin__`):

| Tool | Use |
|---|---|
| `get_inbox` (`limit` 1–50) | Recent conversations: names, previews, dates, and thread references. |
| `get_conversation` (`thread_id`, or `linkedin_username` + `index`) | One thread's messages. Prefer `thread_id` whenever you have one. |
| `search_conversations` (`keywords`, `limit`) | Find threads by a name or keyword. Keep `limit` low. |
| `get_session_status` | Is the saved login alive? No browser needed. |

## Read state

Reading has a side effect. `get_conversation` by **username** and
`search_conversations` click through matching inbox rows, and LinkedIn marks a
clicked row **read**. Opening a thread by `thread_id` marks that thread read too.
`get_inbox` does not. So:

1. Start with `get_inbox`. It is often enough to answer "who wrote".
2. Open only the threads whose text matters, by `thread_id` from the inbox
   references.
3. Tell Ben which threads you opened, because they won't show as unread anymore.

**Never** call `archive_conversation`, `send_message` or `connect_with_person`
from this skill (`send_message` is the only send tool; there is no
`reply_to_conversation`). Replying is **staging-linkedin-message**.

## Login problems

When a tool says it isn't authenticated, setup is in progress, or the session
expired, first run `get_session_status`. Then ask Ben to run
`! uvx mcp-server-linkedin@latest --login` in the prompt. It opens a browser
window for him to sign in, and LinkedIn may ask him to approve it in the mobile
app. Never type credentials. A "browser is busy" error means another Claude
session holds the shared browser, so retry once after a short wait.

If the MCP stays unavailable after that, fall back:

- **Who wrote:** LinkedIn's notification emails (below).
- **What they said:** read the thread in Ben's signed-in Chrome (below).

## Fallback 1: notification emails

| What happened | From | Subject |
|---|---|---|
| A connection messaged Ben | `<Name> via LinkedIn <messaging-digest-noreply@linkedin.com>` | `<First> just messaged you` |
| Someone replied to Ben's InMail | `<Name> via LinkedIn <hit-reply@linkedin.com>` | `<First> replied to your InMail` |
| A recruiter or stranger sent an InMail | `<Name> <inmail-hit-reply@linkedin.com>` | the pitch (often a job title) |

Search them with **searching-inbox-emails** in graph mode:
`from:messaging-digest-noreply@linkedin.com`, `from:hit-reply@linkedin.com`,
`from:inmail-hit-reply@linkedin.com`. The email names the sender but has no
message text. LinkedIn only sends it for a message that has sat unread for a
while, so "no email" means "no *unread* message", not "no reply".

## Fallback 2: Chrome

Read-only. Follow **anthropic-skills:chrome-browser** (load the tools in one
ToolSearch call, `tabs_context_mcp` first, new tab). Open
`https://www.linkedin.com/messaging/`. If you hit a login wall or captcha, stop.
Find the thread with the messaging search box (never the composer), read it with
`get_page_text`, then close the tab. Never type in the composer or click anything
that changes state.

## Resolving a check_linkedin row

The LinkedIn send queue (`outreach linkedin …`, see **staging-linkedin-message**)
marks a send `check_linkedin` when its outcome is unknown: `send_unconfirmed`,
`outcome_unknown`, a sender run that crashed mid-send (`stale_sending`), or an
error that can strike after submission. It is never retried automatically,
because that could deliver it twice. `outreach today` and
`outreach linkedin list --status check_linkedin` show them.

For each one:

1. `outreach linkedin list --status check_linkedin --show-text` for the
   recipient (`linkedin_username`), the approval time and the exact text.
2. Read that person's thread (`get_conversation` with the `linkedin_username`,
   or a `thread_id` from `get_inbox`; it marks the thread read) and look for the
   text after the approval time.
3. It's there: `outreach linkedin result <id> --status sent [--thread-id <id>]`.
   This records the brief or nudge, exactly as a confirmed send would.
4. It's not there: `outreach linkedin requeue <id>` puts it back in line. Only
   requeue once you've seen the thread without it.
5. Can't tell (the MCP is down, the thread won't load): leave it and tell Ben.

## Reporting

Quote each message with its sender and date, then say what it asks of Ben (a
time to pick, a question, or nothing).

## For outreach

1. List the people briefed by LinkedIn: `outreach queue --stage briefed --json`
   (from `~/src/people`, `.venv/bin/python scripts/outreach.py`). Their contact
   docs and `today --json` carry the brief dates.
2. `get_inbox` and match conversation names to those people, on activity after
   each person's brief. For someone who isn't in the inbox page, use
   `get_conversation` with their `linkedin_username` (the slug of
   `linkedin_url`) and accept that this marks the thread read.
3. A LinkedIn reply never records itself. If the thread shows a reply to the
   brief, record it with **working-outreach-queue**:
   `outreach mark <id> replied --channel linkedin --at <time>`, using the
   message's timestamp from the thread.
4. The people DB still gets full thread history only from a data export (for
   `outreach today`'s Fresh section). Ask Ben for one when a broad backfill is
   needed, then follow **importing-linkedin**.
