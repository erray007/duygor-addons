#!/usr/bin/env bash
# Commits registry state (addons/, site/) as github-actions[bot] and pushes it.
# Does nothing when there is no change. Retries once after a rejected pull or push; after a
# successful rebase the index is rebuilt so site/index.json matches the merged addons/.
# Appends `changed=true` to $GITHUB_OUTPUT when something was pushed.
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
if git pull --rebase origin main; then
python -m duygor_registry.cli build-index --out site/index.json
if [ -n "$(git status --porcelain site)" ]; then
git add site
git commit -m "chore: rebuild index after merge [skip ci]"
fi
if git push origin HEAD:main; then
if [ -n "${GITHUB_OUTPUT:-}" ]; then
echo "changed=true" >> "$GITHUB_OUTPUT"
fi
exit 0
fi
echo "push rejected (attempt $attempt)"
else
echo "pull --rebase failed (attempt $attempt)"
git rebase --abort || true
fi
done

echo "could not push the registry update" >&2
exit 1
