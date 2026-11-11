#!/usr/bin/env bash
# Commits registry state (addons/, site/) as github-actions[bot] and pushes it.
# Does nothing when there is no change. Retries once after a non-fast-forward rejection.
# Sets the step output `changed=true` when something was pushed.
set -euo pipefail

if [ -z "$(git status --porcelain addons site)" ]; then
echo "nothing to commit"
exit 0
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add addons site
git commit -m "chore: update registry [skip ci]"

for attempt in 1 2; do
git pull --rebase origin main
if git push origin HEAD:main; then
if [ -n "${GITHUB_OUTPUT:-}" ]; then
echo "changed=true" >> "$GITHUB_OUTPUT"
fi
exit 0
fi
echo "push rejected (attempt $attempt)"
done

echo "could not push the registry update" >&2
exit 1
