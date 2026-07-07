#!/usr/bin/env sh
set -eu

REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"

BACKLOG_PATH="specs/backlog.md"

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
