---
name: staging-linkedin-message
description: >
  Use when Ben wants a LinkedIn message written and ready to paste — "draft a
  LinkedIn message to X", "write it for me" on a LinkedIn brief or nudge, "put
  that on my clipboard", "open LinkedIn so I can send it" — or when a message
  already drafted in the conversation should go out by LinkedIn.
metadata:
  depends-on: "matching-writing-style, working-outreach-queue, fetching-person, anthropic-skills:chrome-browser"
---

# Staging a LinkedIn message

Goal: Ben presses ⌘V and Send, nothing else. Write the message, put it on the
clipboard, and leave LinkedIn open with the person's message box focused.
**Never type into the box and never click Send.** Ben sends.

## 1. The text

- Already drafted in this conversation? Use it as is.
- Otherwise write it in Ben's voice with **matching-writing-style**. For an
  outreach person, write it from the talking points (**working-outreach-queue**
  Rules: write it only because Ben asked). Plain text: LinkedIn renders no
  markdown, so no bullets, bold or bracketed links.

## 2. The clipboard

One Bash call, quoted heredoc so nothing expands:

```bash
pbcopy <<'EOF_MSG'
<the message>
EOF_MSG
```

## 3. The message box

The profile URL: the session snapshot's `LinkedIn:` line, or
**fetching-person** (`linkedin_url`, form `linkedin.com/in/<slug>`).

With Chrome (**anthropic-skills:chrome-browser**), in a new tab:

1. `navigate` to `https://www.linkedin.com/in/<slug>/`.
2. `find` "Message button in the profile header" and click it.
3. Click into the empty "Write a message…" field so it has focus.
4. Screenshot once to confirm the box is open and **empty**. If the profile
   shows "Connect" instead of "Message" (not a 1st-degree connection), stop and
   say so: InMail or a connection note is Ben's call.

Leave the tab open; it is Ben's.

Chrome not connected? `open "https://www.linkedin.com/in/<slug>/"` and tell Ben
to click Message.

## 4. Report

One line — "On your clipboard; <name>'s message box is open in Chrome: ⌘V,
Send." — then the message text, so he can read it first.

For an outreach person, after Ben says it went: record it per
**working-outreach-queue** (`outreach mark <id> briefed --channel linkedin`, or
`outreach nudge <id> --channel linkedin`) and update the contact doc's Log.
