"""Send an iMessage/SMS through Messages.app with osascript. Local only (mcp_servers/imessage.py);
people's Cloud Functions never import this.

The text and the target are passed as osascript arguments (`on run argv`), never spliced into
the script source, so no message text can change what the script does. The first run asks
macOS for permission to control Messages (System Settings → Privacy & Security → Automation).
"""

import subprocess

TIMEOUT_SECONDS = 45
PERMISSION_HINT = (
    "is a permission prompt waiting? (System Settings → Privacy & Security → Automation →"
    " allow your terminal to control Messages)"
)

# An existing conversation, by the chat.db guid (`any;-;+15550100001`, `any;+;chat123…`):
# the same thread Ben sees, on whatever service Messages already uses for it.
_SEND_TO_CHAT = """on run argv
  set theText to item 1 of argv
  set theChat to item 2 of argv
  tell application "Messages"
    with timeout of 30 seconds
      send theText to chat id theChat
    end timeout
  end tell
end run"""

# Nobody to reply to yet: a new conversation over iMessage.
_SEND_TO_HANDLE = """on run argv
  set theText to item 1 of argv
  set theHandle to item 2 of argv
  tell application "Messages"
    with timeout of 30 seconds
      set theService to 1st account whose service type = iMessage
      send theText to participant theHandle of theService
    end timeout
  end tell
end run"""

_CHAT_EXISTS = """on run argv
  tell application "Messages"
    with timeout of 15 seconds
      return id of chat id (item 1 of argv)
    end timeout
  end tell
end run"""


class SendError(Exception):
    """osascript refused or failed: nothing was handed to Messages, or Messages raised."""


class MessagesUnavailable(SendError):
    """Messages never answered (a pending permission prompt, or the app is hung)."""


def _run(script: str, *args: str) -> str:
    try:
        done = subprocess.run(
            ["osascript", "-", *args],
            input=script,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise MessagesUnavailable(f"Messages did not answer — {PERMISSION_HINT}") from e
    if done.returncode != 0:
        err = done.stderr.strip() or f"osascript exited {done.returncode}"
        # -1712: the Apple event timed out; -1743: not authorized to send Apple events.
        if "(-1712)" in err or "(-1743)" in err:
            raise MessagesUnavailable(f"{err} — {PERMISSION_HINT}")
        raise SendError(err)
    return done.stdout.strip()


def chat_known(chat_guid: str) -> bool:
    """Does Messages itself resolve this chat id? Read-only. Raises MessagesUnavailable."""
    try:
        return _run(_CHAT_EXISTS, chat_guid) == chat_guid
    except MessagesUnavailable:
        raise
    except SendError:
        return False


def send_to_chat(text: str, chat_guid: str) -> None:
    _run(_SEND_TO_CHAT, text, chat_guid)


def send_to_handle(text: str, handle: str) -> None:
    _run(_SEND_TO_HANDLE, text, handle)
