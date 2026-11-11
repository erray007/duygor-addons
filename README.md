# duygor-addons

Registry tooling for the DuyGör add-on store. `duygor_registry` validates `.dgp` packages
(zip files) before they are published to the store index. The Android app applies the same
rules, so the two must stay identical.

- `duygor_registry.version`: store version rules (`is_valid`, `compare`, `is_newer`).
- `duygor_registry.package`: `inspect(data: bytes) -> PackageInfo`, raises `PackageError`
  (`code`, `message_tr`, `message_en`) with one of `zip`, `manifest`, `main`, `path`, `size`,
  `id`, `version`, `lang`.
- `duygor_registry.language`: language tag normalization.

- `duygor_registry.github`: minimal GitHub REST client (`GITHUB_TOKEN`, urllib) and
  `parse_release_url`.
- `duygor_registry.registry`: `register`, `remove`, `scan`, `build_index`. State is one record per
  add-on in `addons/<id>.json`; an id belongs forever to the numeric GitHub user id and the
  repository that first registered it.
- `duygor_registry.cli`: what the workflows run:
  - `register --issue-json <path>` / `remove --issue-json <path>`: the file holds
    `{"issue_number", "author_id", "author_login", "body"}`; prints
    `{ok, message_tr, message_en, changed, comment}`.
    `pending` is true when the submission was reserved and awaits approval (see below).
  - `approve --issue-json <path> --sender-id <id>` / `reject ...`: a maintainer's decision on a
    submit issue; prints the same fields plus `authorized` (false, with nothing changed, when the
    sender is not in `config.json` → `maintainers`).
  - `scan`: prints the issues to open as `[{title, body}]`.
  - `build-index --out site/index.json`: `storeEnabled` comes from `config.json`.
- `duygor_registry.trust`: maintainers (`config.json` → `"maintainers"`, numeric GitHub user ids)
  and approved authors (`trusted_authors.json`, a list of `{id, login, approved_at}` sorted by id).
  A malformed file stops the CLI (exit 2) instead of trusting or distrusting anyone.

## First-submission approval

Approval is per author (numeric GitHub user id), not per add-on. Maintainers are always trusted.

- A submit form from an author who is neither a maintainer nor in `trusted_authors.json` runs every
  check; on success the record is saved with `"status": "pending"`, which reserves the id for that
  author and repository but never publishes it. The issue gets a bilingual "awaiting approval"
  comment, the label `onay-bekliyor`, and stays open. Submitting again while pending gives the same
  answer and takes the newer package. A remove form withdraws a pending submission and frees the id.
- Pending records are never scanned, never hidden by the integrity check and never in index.json.
  Records without `status` (all records registered before this feature) are published.
- **Approve:** a maintainer adds the label `onaylandi`. The Review workflow (`issues: labeled`)
  passes the event's `sender.id` to `cli approve`; the author joins `trusted_authors.json` and the
  pending add-on is published from its repository's latest release, checked again now. The issue is
  closed and `onay-bekliyor` removed. If that release fails the checks, the author stays approved,
  the record stays pending and the author's next form publishes it. With nothing pending, the form
  itself is handled as a trusted submission.
- **Reject:** a maintainer adds `reddedildi`; the pending record is deleted (the id is free for
  anyone) and the issue closed. A published record is never touched.
- A decision label from anyone else is removed again, with one comment per issue saying that only
  maintainers can decide. Labels added by bots are ignored.
- From then on the author's new releases (hourly scan) and new add-ons (submit form) publish
  automatically.
- The sweep never resubmits an `onay-bekliyor` issue. An open submit issue that carries a decision
  label (its Review run was lost) is handled by the sweep with the account that last added that
  label, read from the issue's events, and the same maintainer check. If a decision still seems
  stuck, removing and re-adding the label starts a new Review run.

Labels the repository needs: `eklenti-gonder`, `eklenti-kaldir` (forms), `onay-bekliyor`,
`onaylandi`, `reddedildi` (approval).

Store limits on top of the install rules (`registry._inspect`): name at most 100 characters in
every language, author at most 100, description at most 2000, at most 64 language files; such
packages are rejected. Release notes are cut to 10000 characters. Text that cannot be encoded as
UTF-8 (lone surrogates such as `\ud800`) in the name, description, author or release notes is
rejected. Each add-on's index entry, measured exactly as index.json is written (control characters
escaped as `\u00XX` count 6 bytes), may take at most 64 KiB; a larger package is rejected at
register and scan, and a stored record over the budget is skipped by `build-index`. index.json
stays under 2 MiB: when the entries would not fit, `build-index` drops the most recently
registered add-ons until it does, printing `dropped <id>` to stderr, so established add-ons stay
even when they publish updates. The order is each record's `registered_at` (UTC time of its first
successful registration, set once and never changed), then id; records older than that field
use their current Release `published` time. Unreadable or corrupt record files are skipped by
`build-index` (`skipped <file>` on stderr); `scan` still fails on them so they get noticed.

Python 3.11, standard library only.

## Emergency actions

The Pages site is deployed only by the Scan workflow (its `pages` job, after the index was
rebuilt and committed) or by a manual run of the Pages workflow; a plain push does not rebuild
`site/index.json`. So every emergency change is: edit on `main`, push, then run **Actions → Scan →
Run workflow** (workflow_dispatch). The scan rebuilds the index, commits it and deploys it; the
app sees the change on its next store refresh.

- Kill switch (turn the store off for every user): set `"storeEnabled": false` in `config.json`,
  push, run Scan. The index is then built without reading any record (`"addons": []`), so it always
  deploys, whatever state the add-on records are in. It also deploys when the Scan's scan or
  open-issues step fails (a corrupt record, a GitHub outage): the workflow then drops that scan's
  state, still builds, commits and deploys the index from `main`, and the run ends red. A
  `config.json` that cannot be read or parsed, or whose `storeEnabled` is not `true`/`false`,
  closes the store (fail closed) with an `ERROR`/`::error` line in the log; a missing file means
  open. Set it back to `true` the same way to turn the store on again.
- Withdraw an author's approval: delete their entry from `trusted_authors.json`, push. Their new
  add-ons then wait for approval again; already published add-ons stay and keep receiving updates,
  so take them off as below if needed.
- Take one add-on off the store: set `"removed": true` in `addons/<id>.json` (the id stays
  reserved for its owner, who can republish it with the submit form), or delete
  `addons/<id>.json` to free the id entirely; push, run Scan.
- Oversized add-ons never block a deploy: `build-index` skips entries over 64 KiB (`skipped <id>`)
  and drops the most recently registered add-ons while index.json would reach 2 MiB
  (`dropped <id>`), both on the
  "Build the index" step's stderr. If those lines appear, take the spam add-ons off as above so the
  dropped legitimate ones come back on the next Scan. A failure with `index.json would be ... too
  large` can then only come from a broken build (even the empty index too big); the previous index
  stays live.

## Running the tests

```
pip install -r requirements-dev.txt
python -m pytest -q
```
