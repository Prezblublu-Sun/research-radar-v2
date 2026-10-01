#!/usr/bin/env bash
# Push the current HEAD to origin/<branch> with retries (ADR-0030).
#
# The 2026-09-12 daily run lost a full day of data because a single
# `git push` hit a transient GitHub outage ("Failed to connect to
# github.com port 443") and the job exited before anything was published.
# Writers are serialized through the `research-radar-writer` concurrency
# group, so a rejected push is almost always a network blip; when it is a
# genuine non-fast-forward we rebase onto the latest main and try again.
#
# Usage: scripts/git_push_retry.sh [branch] [attempts]
set -uo pipefail

branch="${1:-main}"
attempts="${2:-5}"

for attempt in $(seq 1 "$attempts"); do
  if git push origin "HEAD:${branch}"; then
    echo "push succeeded on attempt ${attempt}"
    exit 0
  fi
  if [ "$attempt" -ge "$attempts" ]; then
    break
  fi
  delay=$((attempt * 30))
  echo "::warning::git push attempt ${attempt}/${attempts} failed; retrying in ${delay}s"
  sleep "$delay"
  # A non-fast-forward rejection means another writer landed first.
  git pull --rebase origin "$branch" || echo "::warning::rebase onto origin/${branch} failed; retrying push as-is"
done

echo "::error::git push failed after ${attempts} attempts; data is committed locally but NOT published"
exit 1
