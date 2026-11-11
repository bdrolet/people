#!/usr/bin/env bash
# Symlink the people consumer skills (and the funded-founder campaign skill) into ~/.claude/skills/ (per-skill
# symlinks — NEVER symlink the parent directory: a v2.1.69 Claude Code
# security fix skips user-level skills entirely when ~/.claude/skills itself
# is a symlink).
# Mirrors ~/src/tasks/scripts/link-skills.sh and ~/src/schedule/scripts/link-skills.sh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/.claude/skills"
mkdir -p "$DEST"

# The outreach skills (working-outreach-queue, checking-linkedin-messages,
# staging-linkedin-message), the outreach-launcher agent and the outreach-sessions
# command are retired: the pipeline moved to pipelines, whose scripts/link-skills.sh
# links its own skills. Remove their old links by hand (pipelines cutover runbook).
for skill in searching-people fetching-person editing-person creating-person working-funded-founder-campaign; do
  # ln -sfn onto a real (non-symlink) directory fails and set -e aborts the
  # loop half-linked; guard so a pre-existing real copy fails loudly instead.
  if [[ -e "$DEST/$skill" && ! -L "$DEST/$skill" ]]; then
    echo "error: $DEST/$skill exists and is not a symlink — remove the old copy first" >&2
    exit 1
  fi
  ln -sfn "$REPO_ROOT/.claude/skills/$skill" "$DEST/$skill"
  echo "linked $DEST/$skill -> $(readlink "$DEST/$skill")"
done

