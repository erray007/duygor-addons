# duygor-addons

Registry tooling for the DuyGör add-on store. `duygor_registry` validates `.dgp` packages
(zip files) before they are published to the store index. The Android app applies the same
rules, so the two must stay identical.

- `duygor_registry.version`: store version rules (`is_valid`, `compare`, `is_newer`).
- `duygor_registry.package`: `inspect(data: bytes) -> PackageInfo`, raises `PackageError`
  (`code`, `message_tr`, `message_en`) with one of `zip`, `manifest`, `main`, `path`, `size`,
  `id`, `version`, `lang`.
- `duygor_registry.language`: language tag normalization.

Python 3.11, standard library only.

## Running the tests

```
pip install -r requirements-dev.txt
python -m pytest -q
```
