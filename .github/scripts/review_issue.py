"""Applies a decision label (onaylandi / reddedildi) to a submit issue; shared by the review workflow and the sweep.

Usage: review_issue.py   reads the issues.labeled event from $GITHUB_EVENT_PATH.

The sweep calls review() for decision-labelled issues whose run was lost, with the account that added
the label taken from the issue's events. Who is a maintainer is decided by the CLI (config.json
maintainers, numeric ids); a label from anyone else is removed again, with one explanatory comment per
issue. Needs GH_TOKEN, GITHUB_TOKEN and GITHUB_REPOSITORY. Event and issue text are only ever data:
read from files, passed to the CLI through a JSON file and to `gh` through body files.
"""

import json
import os
import sys

import process_issue

DENIED_MARKER = "<!-- duygor-review-denied -->"
LABEL_OF = {command: label for label, command in process_issue.DECISIONS.items()}


def remove_label(number, label):
    """Removes the label from the issue; one that is already gone is fine."""
    result = process_issue.gh("api", "--method", "DELETE", "repos/%s/issues/%s/labels/%s" % (process_issue.repo(), number, label), check=False)
    if result.returncode != 0 and "404" not in result.stderr:
        raise RuntimeError("could not remove the label %s: %s" % (label, result.stderr.strip()))


def comment_once(number, marker, text):
    existing = process_issue.gh("api", "--paginate", "repos/%s/issues/%s/comments" % (process_issue.repo(), number), "--jq", ".[].body").stdout
    if marker not in existing:
        process_issue.comment_from_file(number, marker + "\n" + text)


def _is_id(value):
    return isinstance(value, int) and not isinstance(value, bool)


def review(issue, command, sender_id, sender_type):
    """0 when handled or not ours, 1 when it failed (the issue stays open and the sweep retries it)."""
    number = issue.get("number")
    if process_issue.decision_for(issue) != command or sender_type == "Bot" or not _is_id(sender_id):
        sys.stderr.write("issue %s: review skipped\n" % number)
        return 0
    try:
        result = process_issue.run_cli(command, issue, "--sender-id", str(sender_id))
        comment = process_issue.comment_of(result)
        if result.get("authorized") is not True:
            remove_label(number, LABEL_OF[command])
            comment_once(number, DENIED_MARKER, comment)
            return 0
        process_issue.push_registry()
        process_issue.comment_from_file(number, comment)
        remove_label(number, process_issue.PENDING_LABEL)
        process_issue.gh("issue", "close", str(number), "--repo", process_issue.repo())
        return 0
    except Exception as e:
        sys.stderr.write("issue %s: review failed: %s\n" % (number, e))
        process_issue.reset_tree()
        process_issue.post_failure(number)
        return 1


def main():
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
        event = json.load(f)
    command = process_issue.DECISIONS.get((event.get("label") or {}).get("name"))
    number = (event.get("issue") or {}).get("number")
    sender = event.get("sender") or {}
    if command is None or not _is_id(number):
        sys.stderr.write("not a decision label event\n")
        return 0
    # The live issue, so a label removed or an issue closed since the event is not acted on.
    issue = json.loads(process_issue.gh("api", "repos/%s/issues/%s" % (process_issue.repo(), number)).stdout)
    return review(issue, command, sender.get("id"), sender.get("type"))


if __name__ == "__main__":
    sys.exit(main())
