"""Who may publish without review: the maintainers in config.json and the approved authors.

An author is approved once (by numeric GitHub user id), by a maintainer, on their first submission;
from then on their releases and new add-ons publish automatically. The approved authors live in
trusted_authors.json as a list of {id, login, approved_at}, sorted by id. Anything malformed raises
ValueError, so a broken file stops the workflow instead of silently trusting or distrusting anyone.
"""

import json
import os
import tempfile
from datetime import datetime, timezone

CONFIG = "config.json"
TRUSTED = "trusted_authors.json"


def _is_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def maintainers(config_path=CONFIG):
    """The maintainer user ids from config.json; a missing file or key means none."""
    config = _read(config_path, {})
    if not isinstance(config, dict):
        raise ValueError("%s does not hold a JSON object" % config_path)
    ids = config.get("maintainers", [])
    if not isinstance(ids, list) or not all(_is_id(i) for i in ids):
        raise ValueError("%s: maintainers must be a list of numeric GitHub user ids" % config_path)
    return frozenset(ids)


def load(trusted_path=TRUSTED):
    """The approved authors, in file order; a missing file means none."""
    authors = _read(trusted_path, [])
    if not isinstance(authors, list) or not all(isinstance(a, dict) and _is_id(a.get("id")) and isinstance(a.get("login"), str) and isinstance(a.get("approved_at"), str) for a in authors):
        raise ValueError("%s must be a list of {id, login, approved_at}" % trusted_path)
    return authors


def is_trusted(author_id, config_path=CONFIG, trusted_path=TRUSTED):
    return author_id in maintainers(config_path) or any(a["id"] == author_id for a in load(trusted_path))


def _serialize(authors):
    return json.dumps(sorted(authors, key=lambda a: a["id"]), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def add(author_id, login, trusted_path=TRUSTED):
    """Approves the author (keeping the first approval time, following login renames); returns whether the file changed."""
    if not _is_id(author_id):
        raise ValueError("author id must be a positive integer")
    authors = load(trusted_path)
    entry = next((a for a in authors if a["id"] == author_id), None)
    if entry is None:
        authors.append({"id": author_id, "login": login, "approved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    elif entry["login"] != login and login:
        entry["login"] = login
    text = _serialize(authors)
    try:
        with open(trusted_path, encoding="utf-8", newline="") as f:
            if f.read() == text:
                return False
    except FileNotFoundError:
        pass
    directory = os.path.dirname(os.path.abspath(trusted_path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, trusted_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True
