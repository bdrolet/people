#!/usr/bin/env bash
# Install, remove or print the launchd agent that runs the LinkedIn send queue's sender
# (scripts/linkedin_sender.py) every 2 minutes on this Mac.
#
#   scripts/install-linkedin-sender.sh             install (or reinstall) and load it
#   scripts/install-linkedin-sender.sh --uninstall unload it and delete the plist
#   scripts/install-linkedin-sender.sh --print     print the plist, change nothing
#
# The sender reads .env (scripts/fetch-env.sh) from the repo root, like every local
# script. It logs one word per run to ~/Library/Logs/people-linkedin-sender.log and
# never logs message text. Pause it without uninstalling: `outreach linkedin pause`.
set -euo pipefail

LABEL="cloud.drolet.people.linkedin-sender"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/people-linkedin-sender.log"
PYTHON="$REPO_ROOT/.venv/bin/python"
DOMAIN="gui/$(id -u)"

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
    <string>$PYTHON</string>
    <string>$REPO_ROOT/scripts/linkedin_sender.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$REPO_ROOT</string>
  <key>StartInterval</key>
  <integer>120</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>ProcessType</key>
  <string>Background</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StandardOutPath</key>
  <string>$LOG</string>
  <key>StandardErrorPath</key>
  <string>$LOG</string>
</dict>
</plist>
PLIST
}

case "${1:-}" in
  --print)
    plist
    ;;
  --uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed $LABEL"
    ;;
  "")
    [[ -x "$PYTHON" ]] || { echo "error: $PYTHON not found — create the venv first" >&2; exit 1; }
    command -v uvx >/dev/null || [[ -x /opt/homebrew/bin/uvx ]] || {
      echo "error: uvx not found — the sender runs the LinkedIn MCP through it" >&2; exit 1; }
    mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    plist >"$PLIST"
    plutil -lint "$PLIST" >/dev/null
    launchctl bootstrap "$DOMAIN" "$PLIST"
    echo "loaded $LABEL (every 120s); log: $LOG"
    ;;
  *)
    echo "usage: $0 [--print | --uninstall]" >&2
    exit 2
    ;;
esac
