#!/usr/bin/env bash
# Symlink the people consumer skills (and the outreach working skill) into ~/.claude/skills/ (per-skill
# symlinks — NEVER symlink the parent directory: a v2.1.69 Claude Code
# security fix skips user-level skills entirely when ~/.claude/skills itself
# is a symlink).
# Mirrors ~/src/tasks/scripts/link-skills.sh and ~/src/schedule/scripts/link-skills.sh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/.claude/skills"
mkdir -p "$DEST"

for skill in searching-people fetching-person editing-person creating-person working-outreach-queue working-funded-founder-campaign checking-linkedin-messages staging-linkedin-message; do
  # ln -sfn onto a real (non-symlink) directory fails and set -e aborts the
  # loop half-linked; guard so a pre-existing real copy fails loudly instead.
  if [[ -e "$DEST/$skill" && ! -L "$DEST/$skill" ]]; then
    echo "error: $DEST/$skill exists and is not a symlink — remove the old copy first" >&2
    exit 1
  fi
  ln -sfn "$REPO_ROOT/.claude/skills/$skill" "$DEST/$skill"
  echo "linked $DEST/$skill -> $(readlink "$DEST/$skill")"
done

# Agents into ~/.claude/agents/ (per-file symlinks, as the tasks repo does).
AGENT_DIR="$HOME/.claude/agents"
mkdir -p "$AGENT_DIR"
for agent in outreach-launcher; do
  if [[ -e "$AGENT_DIR/$agent.md" && ! -L "$AGENT_DIR/$agent.md" ]]; then
    echo "error: $AGENT_DIR/$agent.md exists and is not a symlink — remove the old copy first" >&2
    exit 1
  fi
  ln -sfn "$REPO_ROOT/.claude/agents/$agent.md" "$AGENT_DIR/$agent.md"
  echo "linked $AGENT_DIR/$agent.md -> $(readlink "$AGENT_DIR/$agent.md")"
done

# outreach-sessions onto PATH: one background Claude session per outreach person.
BIN_DEST="$HOME/.local/bin"
mkdir -p "$BIN_DEST"
if [[ -e "$BIN_DEST/outreach-sessions" && ! -L "$BIN_DEST/outreach-sessions" ]]; then
  echo "error: $BIN_DEST/outreach-sessions exists and is not a symlink — remove the old copy first" >&2
  exit 1
fi
ln -sfn "$REPO_ROOT/scripts/outreach-sessions" "$BIN_DEST/outreach-sessions"
echo "linked $BIN_DEST/outreach-sessions -> $(readlink "$BIN_DEST/outreach-sessions")"
