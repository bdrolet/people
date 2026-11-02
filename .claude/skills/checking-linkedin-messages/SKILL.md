---
name: checking-linkedin-messages
description: >
  Use when the user asks whether anyone messaged them on LinkedIn — "did anyone
  reply on LinkedIn", "check my LinkedIn messages", "any LinkedIn replies to my
  outreach", "who messaged me on LinkedIn", "what did X say on LinkedIn" — or when
  outreach briefs went out by LinkedIn and there is no fresh LinkedIn data export
  to import. Finds who wrote from notification emails, then reads what they said
  in Ben's signed-in Chrome.
metadata:
  depends-on: "searching-inbox-emails, fetching-inbox-email, working-outreach-queue, importing-linkedin, anthropic-skills:chrome-browser"
---

# Checking LinkedIn messages

There is no LinkedIn API here. The people DB only sees LinkedIn messages from a
data export (**importing-linkedin**), which goes stale between exports. In
between, there are two sources:

- **Who wrote:** LinkedIn's **notification emails** in Ben's primary mailbox.
  Search them with **searching-inbox-emails** in graph mode (below).
- **What they said:** the thread itself, read in **Ben's signed-in Chrome**
  (see "Reading the thread in Chrome"). Do this whenever the text matters, such
  as a reply to outreach or a scheduling message. Don't hand it back to Ben as
  "open LinkedIn to read it" unless Chrome fails.

## The senders

| What happened | From | Subject |
|---|---|---|
| A connection messaged Ben | `<Name> via LinkedIn <messaging-digest-noreply@linkedin.com>` | `<First> just messaged you` |
| Someone replied to Ben's InMail | `<Name> via LinkedIn <hit-reply@linkedin.com>` | `<First> replied to your InMail` |
| A recruiter or stranger sent an InMail | `<Name> <inmail-hit-reply@linkedin.com>` | the pitch (often a job title) |
| Noise: job insights, "X is hiring", digests | `LinkedIn <messages-noreply@linkedin.com>` | varies |
| Noise: security | `LinkedIn <security-noreply@linkedin.com>` | varies |

Queries:

```
from:messaging-digest-noreply@linkedin.com
from:hit-reply@linkedin.com
from:inmail-hit-reply@linkedin.com
```

Run all three and keep results newer than the date that matters, such as the
brief date for an outreach reply check. The sender name is in `sender_display`
(`<Name> via LinkedIn`), and `messages-noreply` never carries a person's message.

## What it can and can't tell you

- **Who, not what.** The email names the sender and their headline. The message
  text is not in it ("View message"); **fetching-inbox-email** on the hit shows
  only that. Read the text in Chrome.
- **Unread only, and late.** LinkedIn emails about a message only when it has sat
  unread for a while. A message Ben already read in the app never sends one. So
  "no email" means "no *unread* message", not "no reply". When it matters whether
  a specific person replied, check their thread in Chrome rather than relying on
  the absence of an email.
- **Rare.** Expect a handful a month, not one per message.

## Reading the thread in Chrome

Read-only. Follow **anthropic-skills:chrome-browser** for the tool mechanics:
load the Chrome tools in one ToolSearch call, call `tabs_context_mcp` first, and
work in a new tab.

1. Open `https://www.linkedin.com/messaging/` in a new tab.
2. Signed out, a login wall, or a captcha → stop. Tell Ben to sign in to LinkedIn
   in Chrome. Never type credentials.
3. Find the person's thread: click it in the conversation list, or type their
   name into the **messaging search box** (the box above the list, never the
   message composer) and open the match. With two people of the same name, use
   the headline from the notification email to pick the right one.
4. Read the thread with `get_page_text` (or `read_page`). Take the messages since
   the date that matters, each with its sender and timestamp. Scroll up in the
   thread if it starts after that date.
5. Report each message quoted, with sender and date, then what it asks of Ben
   (a time to pick, a question, nothing).
6. Close the tab.

**Never** type in the message composer, send, react, accept or ignore an
invitation, archive, or mark anything unread or read on purpose. Opening a
thread marks it read in LinkedIn. That's expected, but say which threads you
opened so Ben knows they won't show as unread.

If Chrome is unavailable (no extension, tools fail twice, or LinkedIn won't
load), fall back to "<Name> messaged you on LinkedIn on <date>; open LinkedIn
to read it".

## For outreach

1. List the people briefed by LinkedIn: `outreach queue --stage briefed --json`
   (from `~/src/people`, `.venv/bin/python scripts/outreach.py`). Their contact
   docs and `today --json` carry the brief dates.
2. Match notification senders (`<Name> via LinkedIn`) to those names, on emails
   received after each person's brief.
3. Read each matched thread in Chrome (above), and report what each person said.
   A LinkedIn reply never records itself. If the thread shows a reply to the
   brief, record it with **working-outreach-queue**:
   `outreach mark <id> replied --channel linkedin --at <time>`, using the
   message's timestamp from the thread (or the email's `received_at` if Chrome
   fell back).
4. A full export is still how the people DB gets every thread's history for
   `outreach today`'s Fresh section. Ask Ben for one when a broad backfill is
   needed, then follow **importing-linkedin**.
