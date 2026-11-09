#!/usr/bin/env bash
# Install the WhatsApp MCP server (github.com/lharries/whatsapp-mcp) at a pinned commit, with
# this repo's patch, and keep its bridge running as a launchd agent on this Mac.
#
#   scripts/install-whatsapp-mcp.sh              install (or reinstall) and load the bridge agent
#   scripts/install-whatsapp-mcp.sh --login      stop the agent, run the bridge in the foreground
#                                                to scan the QR code, then start the agent again
#   scripts/install-whatsapp-mcp.sh --status     is the agent loaded, is the bridge answering
#   scripts/install-whatsapp-mcp.sh --uninstall  unload the agent and delete the plist (keeps
#                                                ~/.whatsapp-mcp, which holds the session and messages)
#   scripts/install-whatsapp-mcp.sh --print      print the plist, change nothing
#
# Two parts: a Go bridge (whatsmeow, signed in as a linked device like WhatsApp Web) that
# stores messages in ~/.whatsapp-mcp/src/whatsapp-bridge/store/messages.db and serves a send
# API on 127.0.0.1, and a Python MCP server (.mcp.json `whatsapp`) that reads that store and
# calls the API. The MCP server can't send while the bridge is down.
#
# scripts/whatsapp-mcp/whatsapp-mcp.patch, applied on top of the pinned commit:
#   - the send API listens on 127.0.0.1 only (upstream: every interface, no auth), on
#     WHATSAPP_BRIDGE_PORT (default 18765; upstream's 8080 is people-api's dev port)
#   - it refuses a request with an Origin header or a non-JSON body, so a web page can't send
#   - the bridge never logs message text (upstream logs every message)
#   - whatsmeow bumped to a current version (the pinned one is rejected by WhatsApp)
# Bumping PINNED_COMMIT means regenerating the patch against it.
set -euo pipefail

UPSTREAM="https://github.com/lharries/whatsapp-mcp.git"
PINNED_COMMIT="7d6a06dcdce1f01dfb24f60e1030d5efba9f3b88"
LABEL="cloud.drolet.people.whatsapp-bridge"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PATCH="$REPO_ROOT/scripts/whatsapp-mcp/whatsapp-mcp.patch"
HOME_DIR="$HOME/.whatsapp-mcp"
SRC="$HOME_DIR/src"
BRIDGE_DIR="$SRC/whatsapp-bridge"
BIN="$HOME_DIR/bin/whatsapp-bridge"
PORT="${WHATSAPP_BRIDGE_PORT:-18765}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/people-whatsapp-bridge.log"
DOMAIN="gui/$(id -u)"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

plist() {
  cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$BIN</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$BRIDGE_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>300</integer>
  <key>ProcessType</key>
  <string>Background</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>WHATSAPP_BRIDGE_PORT</key>
    <string>$PORT</string>
  </dict>
  <key>StandardOutPath</key>
  <string>$LOG</string>
  <key>StandardErrorPath</key>
  <string>$LOG</string>
</dict>
</plist>
PLIST
}

checkout() {
  command -v go >/dev/null || { echo "error: go not found — brew install go" >&2; exit 1; }
  command -v uv >/dev/null || { echo "error: uv not found — brew install uv" >&2; exit 1; }
  mkdir -p "$HOME_DIR/bin"
  [[ -d "$SRC/.git" ]] || git clone -q "$UPSTREAM" "$SRC"
  git -C "$SRC" fetch -q origin
  # -f discards the previously applied patch; store/ (session + messages) is untracked and kept.
  git -C "$SRC" checkout -q -f "$PINNED_COMMIT"
  git -C "$SRC" apply "$PATCH"
  (cd "$BRIDGE_DIR" && go build -o "$BIN" .)
  uv --directory "$SRC/whatsapp-mcp-server" sync --frozen -q
  echo "built $BIN at $PINNED_COMMIT (+ patch)"
}

load() {
  mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  plist >"$PLIST"
  plutil -lint "$PLIST" >/dev/null
  launchctl bootstrap "$DOMAIN" "$PLIST"
  echo "loaded $LABEL (127.0.0.1:$PORT); log: $LOG"
}

logged_in() {
  # whatsmeow keeps the paired device in store/whatsapp.db.
  [[ -f "$BRIDGE_DIR/store/whatsapp.db" ]] &&
    [[ -n "$(sqlite3 -readonly "$BRIDGE_DIR/store/whatsapp.db" 'SELECT jid FROM whatsmeow_device LIMIT 1' 2>/dev/null)" ]]
}

case "${1:-}" in
  --print)
    plist
    ;;
  --uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed $LABEL (kept $HOME_DIR)"
    ;;
  --login)
    [[ -x "$BIN" ]] || checkout
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    echo "WhatsApp on your phone → Settings → Linked devices → Link a device, then scan."
    echo "Once it says 'Connected to WhatsApp', wait for the history sync to settle, then Ctrl+C."
    (cd "$BRIDGE_DIR" && WHATSAPP_BRIDGE_PORT="$PORT" "$BIN") || true
    load
    ;;
  --status)
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then echo "agent: loaded"; else echo "agent: not loaded"; fi
    if logged_in; then echo "session: paired"; else echo "session: not paired — run --login"; fi
    # An empty POST is refused with 400 by a live bridge; no answer means it's down.
    code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
      -d '{}' "http://127.0.0.1:$PORT/api/send" || true)
    if [[ "$code" == "400" ]]; then echo "bridge: answering on 127.0.0.1:$PORT"; else echo "bridge: not answering (HTTP $code)"; fi
    ;;
  "")
    checkout
    load
    logged_in || echo "not paired yet: run scripts/install-whatsapp-mcp.sh --login"
    ;;
  *)
    echo "usage: $0 [--login | --status | --print | --uninstall]" >&2
    exit 2
    ;;
esac
