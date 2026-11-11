"""Command line entry points the GitHub Actions workflows run.

register/remove: read the issue (JSON file with issue_number, author_id, author_login, body),
print {ok, message_tr, message_en, changed, comment} for the workflow to post.
scan: print the issues to open as a JSON list of {title, body}; nothing is opened here.
build-index: write the index.json the app downloads.
Handled outcomes always exit with 0; only broken input or state exits non-zero.
"""

import argparse
import json
import os
import re
import sys

from . import registry
from .registry import Outcome

# Field labels of the issue forms (.github/ISSUE_TEMPLATE); the forms must use exactly these.
SUBMIT_URL = "Release adresi (Release URL)"
SUBMIT_RESPONSIBILITY = "Sorumluluk (Responsibility)"
REMOVE_ID = "Eklenti kimliği (Add-on id)"
REMOVE_CONFIRM = "Onay (Confirmation)"
NO_RESPONSE = "_No response_"

_HEADING = "### "
_CHECKBOX = re.compile(r"\s*[-*] \[([ xX])\]")


def parse_sections(body):
    """Issue form body -> {heading: value}; the first occurrence of a heading wins."""
    sections = {}
    heading = None
    lines = []
    for line in (body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith(_HEADING):
            if heading is not None:
                sections.setdefault(heading, "\n".join(lines).strip())
            heading = line[len(_HEADING):].strip()
            lines = []
        elif heading is not None:
            lines.append(line)
    if heading is not None:
        sections.setdefault(heading, "\n".join(lines).strip())
    return sections


def field(sections, heading):
    """A text field's value; "" when missing or left empty."""
    value = sections.get(heading, "").strip()
    return "" if value == NO_RESPONSE else value


def is_checked(value):
    """True when the section has checkboxes and every one of them is ticked."""
    boxes = [m.group(1) for m in (_CHECKBOX.match(line) for line in value.split("\n")) if m]
    return bool(boxes) and all(b in "xX" for b in boxes)


def _missing(heading):
    return Outcome(False, None, None, "Formdaki '%s' alanı boş ya da eksik." % heading, "The form field '%s' is empty or missing." % heading)


def _unchecked(heading):
    return Outcome(False, None, None, "Formdaki '%s' kutusu işaretlenmemiş." % heading, "The box '%s' in the form is not ticked." % heading)


def parse_submit(body):
    """-> (release_url, None) or (None, rejection Outcome)."""
    sections = parse_sections(body)
    url = field(sections, SUBMIT_URL).split("\n", 1)[0].strip()
    if not url:
        return None, _missing(SUBMIT_URL)
    if not is_checked(sections.get(SUBMIT_RESPONSIBILITY, "")):
        return None, _unchecked(SUBMIT_RESPONSIBILITY)
    return url, None


def parse_remove(body):
    """-> (addon_id, None) or (None, rejection Outcome)."""
    sections = parse_sections(body)
    addon_id = field(sections, REMOVE_ID).split("\n", 1)[0].strip().strip("`").strip()
    if not addon_id:
        return None, _missing(REMOVE_ID)
    if not is_checked(sections.get(REMOVE_CONFIRM, "")):
        return None, _unchecked(REMOVE_CONFIRM)
    return addon_id, None


def _read_issue(path):
    with open(path, encoding="utf-8") as f:
        issue = json.load(f)
    author_id = issue.get("author_id") if isinstance(issue, dict) else None
    if not isinstance(author_id, int) or isinstance(author_id, bool):
        raise ValueError("issue JSON needs an integer author_id")
    body = issue.get("body")
    return author_id, str(issue.get("author_login") or ""), body if isinstance(body, str) else ""


def _print(value):
    sys.stdout.write(json.dumps(value, sort_keys=True) + "\n")


def _print_outcome(outcome):
    _print({"ok": outcome.ok, "message_tr": outcome.message_tr, "message_en": outcome.message_en, "changed": outcome.changed, "comment": outcome.message_tr + "\n\n" + outcome.message_en})


class IssueRecorder:
    """Wraps a GitHub client and records open_issue calls instead of opening issues."""

    def __init__(self, gh):
        self._gh = gh
        self.issues = []

    def __getattr__(self, name):
        return getattr(self._gh, name)

    def open_issue(self, title, body):
        self.issues.append({"title": title, "body": body})
        return 0


def _client(gh):
    if gh is not None:
        return gh
    from .github import GitHub
    return GitHub()


def _store_enabled(path):
    """storeEnabled from the config; a missing file means true. The maintainer only edits the file to
    close the store, so anything unreadable or malformed fails closed, reported on stderr and as a
    GitHub Actions error annotation."""
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        return True
    except (OSError, ValueError) as e:
        problem = "cannot be read (%s: %s)" % (type(e).__name__, e)
    else:
        if not isinstance(config, dict):
            problem = "does not hold a JSON object"
        else:
            enabled = config.get("storeEnabled", True)
            if isinstance(enabled, bool):
                return enabled
            problem = "has a storeEnabled that is not true or false"
    message = "%s %s, so the store is closed (storeEnabled false) until it is fixed" % (path, problem)
    sys.stderr.write("ERROR: %s\n" % message)
    print("::error title=config.json::" + " ".join(message.split()))
    return False


def _write_if_changed(path, text):
    data = text.encode("utf-8")
    try:
        with open(path, "rb") as f:
            if f.read() == data:
                return False
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return True


def _parser():
    parser = argparse.ArgumentParser(prog="duygor_registry.cli", description="DuyGör add-on store registry")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("register", "remove"):
        p = sub.add_parser(name)
        p.add_argument("--issue-json", required=True)
        p.add_argument("--addons", default="addons")
    p = sub.add_parser("scan")
    p.add_argument("--addons", default="addons")
    p = sub.add_parser("build-index")
    p.add_argument("--out", required=True)
    p.add_argument("--addons", default="addons")
    p.add_argument("--config", default="config.json")
    return parser


def main(argv=None, gh=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == "register":
            author_id, author_login, body = _read_issue(args.issue_json)
            url, outcome = parse_submit(body)
            if outcome is None:
                outcome = registry.register(_client(gh), args.addons, url, author_id, author_login)
            _print_outcome(outcome)
        elif args.command == "remove":
            author_id, _, body = _read_issue(args.issue_json)
            addon_id, outcome = parse_remove(body)
            if outcome is None:
                outcome = registry.remove(_client(gh), args.addons, addon_id, author_id)
            _print_outcome(outcome)
        elif args.command == "scan":
            recorder = IssueRecorder(_client(gh))
            for outcome in registry.scan(recorder, args.addons):
                sys.stderr.write("%s %s\n" % ("ok" if outcome.ok else "problem", outcome.message_en))
            _print(recorder.issues)
        else:
            index = registry.build_index(args.addons, _store_enabled(args.config))
            if _write_if_changed(args.out, registry.index_json(index)):
                sys.stderr.write("wrote %s (%d add-ons)\n" % (args.out, len(index["addons"])))
    except (OSError, ValueError) as e:
        sys.stderr.write("error: %s\n" % e)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
