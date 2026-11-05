---
name: staging-linkedin-message
description: >
  Use when Ben wants a LinkedIn message written and sent or ready to send —
  "draft a LinkedIn message to X", "write it for me" on a LinkedIn brief or
  nudge, "reply to X on LinkedIn", "send that on LinkedIn", "put that on my
  clipboard", "open LinkedIn so I can send it" — or when a message already
  drafted in the conversation should go out by LinkedIn.
metadata:
  depends-on: "matching-writing-style, working-outreach-queue, fetching-person, checking-linkedin-messages, anthropic-skills:chrome-browser"
---

# Staging (and sending) a LinkedIn message

Write the message, show it to Ben, and once he approves **that exact text**, put
it in the **LinkedIn send queue** (`outreach linkedin …` in `~/src/people`). A
background sender on Ben's Mac sends queued messages one at a time, spaced 3–8
minutes apart, weekdays 8 AM–7 PM Pacific, under a daily and weekly ceiling. That
pacing protects his account: never send several approved messages back to back
yourself. Every LinkedIn send gets a row, including ones Ben makes by hand.

Run the CLI from `~/src/people` as `.venv/bin/python scripts/outreach.py linkedin …`
(shortened to `outreach linkedin …` below).

## 1. The text

- Already drafted in this conversation? Use it as is.
- Otherwise write it in Ben's voice with **matching-writing-style**. For an
  outreach person, write it from the talking points (**working-outreach-queue**
  Rules: write it only because Ben asked). Plain text: LinkedIn renders no
  markdown, so no bullets, bold or bracketed links. No tab characters.

## 2. Recipient and thread

`send_message(linkedin_username=<slug>, ...)` is the **only** send tool. There is
no `reply_to_conversation`. The slug comes from the session snapshot's
`LinkedIn:` line, or from **fetching-person** (`linkedin_url`, form
`linkedin.com/in/<slug>`); `--person` finds it for you.

For a reply, note the thread's `thread_id` (from `get_inbox` /
`get_conversation`) and pass it as `--thread-id`. `send_message` targets the
profile, which for a 1st-degree connection normally opens the existing 1:1
thread, but it may open a separate DM. The queue keeps the `thread_id` and flags
a send that landed in a different thread (`thread_mismatch`). InMail and
recruiter threads have no Message action, so a reply there is set aside: tell Ben
he has to answer it himself.

## 3. Dry run, then approval

1. Call `mcp__linkedin__send_message` with `confirm_send: false`. This checks the
   recipient and the composer without typing anything, and reports who would
   receive it.
2. Show Ben the recipient the dry run named, and the **exact** message text, and
   ask whether to send it. Stop and wait for his answer.
3. Watch for these statuses:
   - `enter_to_send_enabled`: relay the instructions (switch LinkedIn's setting
     to "Click Send to send"), then retry the dry run.
   - `message_unavailable` (not a 1st-degree connection, no Message action):
     stop. Whether to send InMail or a connection note is Ben's call.
   - `composer_occupied`: a draft is already in that thread. Leave it alone and
     tell him.

## 4. Queue it (the default)

After Ben's explicit yes to that text ("send it", "yes", "go"), write the exact
approved text to a file and queue it:

```bash
cat > /tmp/li-msg.txt <<'EOF_MSG'
<the approved message>
EOF_MSG
outreach linkedin queue --person <ident> --kind brief|nudge|reply|other \
  --message-file /tmp/li-msg.txt [--thread-id <id>] [--not-before "2026-11-06T08:30-08:00"]
```

Use `--username <slug>` instead of `--person` for someone with no `people` row.
`--kind` is Ben's intent; the pipeline event (brief for `to_contact`, nudge for
`briefed`) is decided when it actually sends. The command prints the row id and
the expected send time: report "Queued for <name>, expected <time>." If Ben edits
the text, show the new version and get a new yes before queuing. Approval for one
message never covers another. A message queued but not yet sent can be changed
with `outreach linkedin edit <id> --message-file …` (a new approval) or dropped
with `outreach linkedin cancel <id>`.

## 5. Send now (when Ben asks, or a live back-and-forth)

When Ben says "send it now", or it is a reply in a live conversation:

1. `outreach linkedin queue … --send-now`. It refuses (exit 1, nothing written)
   once today's or this week's ceiling is reached. Tell Ben, and offer to queue
   it instead.
2. Otherwise it prints the exact `send_message` arguments. Call
   `mcp__linkedin__send_message` with **exactly** those (they are the approved
   text, normalized the way LinkedIn sends it), `confirm_send: true`.
3. Report what came back, verbatim:
   `outreach linkedin result <id> --mcp-result '<the JSON it returned>'`.
   This applies the same rules as the background sender: it records the pipeline
   event on `sent`, sets the row aside or marks it `check_linkedin` otherwise,
   and pauses the queue if LinkedIn pushed back.
4. Tell Ben the outcome. On `check_linkedin` (`retry_safe: false` or
   `outcome_unknown`): **do not retry**, it may deliver twice. Follow
   **checking-linkedin-messages** → "Resolving a check_linkedin row".

## 6. Ben sends it himself (fallback or on request)

When he asks to send it himself, or the MCP is unavailable (see
**checking-linkedin-messages** → Login problems):

```bash
pbcopy <<'EOF_MSG'
<the message>
EOF_MSG
```

Then open the message box with Chrome (**anthropic-skills:chrome-browser**, new
tab): `navigate` to `https://www.linkedin.com/in/<slug>/`, click the profile's
Message button, and click into the empty "Write a message…" field. Screenshot
once to confirm it's empty. **Never type into it or click Send.** If Chrome isn't
connected, run `open "https://www.linkedin.com/in/<slug>/"` instead. Report:
"On your clipboard; <name>'s message box is open in Chrome: ⌘V, Send." followed
by the text.

Once Ben says he sent it, record it (this counts against the ceiling and records
the brief or nudge for an outreach person):

```bash
outreach linkedin record --person <ident> --kind brief|nudge|reply|other \
  --message-file /tmp/li-msg.txt [--at <when he sent it>]
```

## 7. After it's sent (outreach people)

Queued and send-now messages record their own pipeline event when they go out,
and `record` does for hand sends: don't also run `outreach mark` or
`outreach nudge` for a LinkedIn message. Update the contact doc's Log once it has
gone out. `outreach linkedin list` shows what's waiting, set aside, or needs a
check; `outreach linkedin status` shows the ceiling and any pause.
