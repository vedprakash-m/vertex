#!/usr/bin/env sh
set -eu

REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"

# BL-K1 (2026-07-22): specs/bklg.md is the tracked canonical file (real git
# history from here on); specs/backlog.md is the actively-edited, gitignored
# working copy it's periodically resynced from -- it was never tracked, so
# `git cat-file -e HEAD:specs/backlog.md` always failed and this guard was a
# silent no-op. Pointed at the file that actually has history to compare
# against.
BACKLOG_PATH="specs/bklg.md"

if git cat-file -e "HEAD:${BACKLOG_PATH}" >/dev/null 2>&1; then
  head_lines=$(git show "HEAD:${BACKLOG_PATH}" | wc -l | tr -d ' ')
  current_lines=$(wc -l < "${BACKLOG_PATH}" | tr -d ' ')
  if [ "$head_lines" -gt 0 ]; then
    min_allowed=$(( (head_lines + 1) / 2 ))
    if [ "$current_lines" -lt "$min_allowed" ]; then
      echo "Backlog Protection: ${BACKLOG_PATH} dropped from ${head_lines} lines to ${current_lines} lines (>50% vs HEAD)." >&2
      echo "Re-verify the restored content against current code state before committing." >&2
    fi
  fi
fi

python scripts/check_backlog_citations.py "$BACKLOG_PATH"
