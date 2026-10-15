---
name: checking-linkedin-messages
description: >
  Use when the user asks whether anyone messaged them on LinkedIn — "did anyone
  reply on LinkedIn", "check my LinkedIn messages", "any LinkedIn replies to my
  outreach", "who messaged me on LinkedIn" — or when outreach briefs went out by
  LinkedIn and there is no fresh LinkedIn data export to import.
metadata:
  depends-on: "searching-inbox-emails, fetching-inbox-email, working-outreach-queue, importing-linkedin"
---

# Checking LinkedIn messages

There is no LinkedIn API here. The people DB only sees LinkedIn messages from a
data export (**importing-linkedin**), which goes stale between exports. In
between, the signal is LinkedIn's **notification emails**, which land in Ben's
primary mailbox. Search them with **searching-inbox-emails** in graph mode.

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
  only that. To know what they said, Ben opens LinkedIn, or a later export brings
  the text in.
- **Unread only, and late.** LinkedIn emails about a message only when it has sat
  unread for a while. A message Ben already read in the app never sends one. So
  "no email" means "no *unread* message", not "no reply". Say so.
- **Rare.** Expect a handful a month, not one per message.

## For outreach

1. List the people briefed by LinkedIn: `outreach queue --stage briefed --json`
   (from `~/src/people`, `.venv/bin/python scripts/outreach.py`). Their contact
   docs and `today --json` carry the brief dates.
2. Match notification senders (`<Name> via LinkedIn`) to those names, on emails
   received after each person's brief.
3. Report matches as "<Name> messaged you on LinkedIn on <date>; open LinkedIn to
   read it". A LinkedIn reply never records itself. After Ben confirms it's a
   reply to the brief, record it with **working-outreach-queue**:
   `outreach mark <id> replied --channel linkedin --at <time>`, using the email's
   `received_at`.
4. For the text of every LinkedIn thread, and to bring replies into `outreach
   today`'s Fresh section, ask Ben for a new data export, then follow
   **importing-linkedin**.
