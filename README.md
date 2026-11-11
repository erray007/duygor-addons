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

Python 3.11, standard library only.

## Running the tests

```
pip install -r requirements-dev.txt
python -m pytest -q
```
