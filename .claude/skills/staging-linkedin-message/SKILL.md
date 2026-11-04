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

Write the message, show it to Ben, and send it through the **`linkedin` MCP
server** (`mcp__linkedin__*`, see **checking-linkedin-messages**) **only after
he approves that exact text**. If he'd rather send it himself, use the
clipboard path in step 5.

## 1. The text

- Already drafted in this conversation? Use it as is.
- Otherwise write it in Ben's voice with **matching-writing-style**. For an
  outreach person, write it from the talking points (**working-outreach-queue**
  Rules: write it only because Ben asked). Plain text: LinkedIn renders no
  markdown, so no bullets, bold or bracketed links. No tab characters.

## 2. New message or reply?

- **They already have a thread with Ben** (anything in `get_inbox`, an InMail,
  or a reply you're answering): reply in that thread with
  `reply_to_conversation(thread_id, ...)`. Never use `send_message` to answer an
  existing thread, because it can open a separate DM.
- **No thread yet:** use `send_message(linkedin_username=<slug>, ...)`. The slug
  comes from the session snapshot's `LinkedIn:` line, or from **fetching-person**
  (`linkedin_url`, form `linkedin.com/in/<slug>`).

## 3. Dry run, then approval

1. Call the tool with `confirm_send: false`. This checks the recipient and the
   composer without typing anything, and reports who would receive it.
2. Show Ben the recipient the dry run named, and the **exact** message text, and
   ask whether to send it. Stop and wait for his answer.
3. Watch for these statuses:
   - `enter_to_send_enabled`: relay the instructions (switch LinkedIn's setting
     to "Click Send to send"), then retry the dry run.
   - Not a 1st-degree connection (no Message action): stop. Whether to send
     InMail or a connection note is Ben's call.
   - `composer_occupied`: a draft is already in that thread. Leave it alone and
     tell him.

## 4. Send

Only after Ben's explicit yes to that text ("send it", "yes", "go"), call again
with `confirm_send: true` and the **identical** `message`. If Ben edits the text,
show the new version and get a new yes before sending. Approval for one message
never covers another.

Check the result:

- `sent: true`: done. Report "Sent to <name> on LinkedIn."
- `sent: false` with `retry_safe: true`: nothing went out. Fix the problem and
  dry-run again.
- `retry_safe: false` or `status: outcome_unknown`: **do not retry**, because it
  may deliver twice. Read the thread with `get_conversation(thread_id)` to see
  whether the message is there, and tell Ben what you found.

## 5. Ben sends it himself (fallback or on request)

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

## 6. Record it (outreach people)

Once it's sent (confirmed by `sent: true`, or Ben says he sent it), record it per
**working-outreach-queue**: `outreach mark <id> briefed --channel linkedin`, or
`outreach nudge <id> --channel linkedin`, and update the contact doc's Log.
