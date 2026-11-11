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
  - `scan`: prints the issues to open as `[{title, body}]`.
  - `build-index --out site/index.json`: `storeEnabled` comes from `config.json`.

Store limits on top of the install rules (`registry._inspect`): name at most 100 characters in
every language, author at most 100, description at most 2000, at most 64 language files; such
packages are rejected. Release notes are cut to 10000 characters. `build-index` fails (exit 2,
nothing written) when index.json would reach 2 MB, so an oversized index is never published.

Python 3.11, standard library only.

## Emergency actions

The Pages site is deployed only by the Scan workflow (its `pages` job, after the index was
rebuilt and committed) or by a manual run of the Pages workflow; a plain push does not rebuild
`site/index.json`. So every emergency change is: edit on `main`, push, then run **Actions → Scan →
Run workflow** (workflow_dispatch). The scan rebuilds the index, commits it and deploys it; the
app sees the change on its next store refresh.

- Kill switch (turn the store off for every user): set `"storeEnabled": false` in `config.json`,
  push, run Scan. Set it back to `true` the same way to turn the store on again.
- Take one add-on off the store: set `"removed": true` in `addons/<id>.json` (the id stays
  reserved for its owner, who can republish it with the submit form), or delete
  `addons/<id>.json` to free the id entirely; push, run Scan.
- If the Scan's "Build the index" step fails with `index.json would be ... too large`, nothing was
  published and the previous index stays live. Remove the largest add-ons as above in the same
  push as any other change, then run Scan again.

## Running the tests

```
pip install -r requirements-dev.txt
python -m pytest -q
```
