"""Applies a decision label (onaylandi / reddedildi) to a submit issue; shared by the review workflow and the sweep.

Usage: review_issue.py   reads the issues.labeled event from $GITHUB_EVENT_PATH.

Who added the label comes from the issue's events (labeler): only an addition that still stands
counts, i.e. the label was not removed and the issue was not closed or reopened after it, so an old
decision never replays on a reopened issue. On the event path that account must also be the event's
sender. The account's maintainer status (config.json maintainers, numeric ids) is checked first: a
label from anyone else is removed again, with one explanatory comment per issue, before anything else
about the issue is looked at. A maintainer's label acts only on an issue awaiting approval
(onay-bekliyor) with exactly one decision label. Every finished decision closes the issue and drops
its approval labels. Needs GH_TOKEN, GITHUB_TOKEN and GITHUB_REPOSITORY. Event and issue text are only
ever data: read from files, passed to the CLI through a JSON file and to `gh` through body files.
"""

import json
import os
import sys

import process_issue

DENIED_MARKER = "<!-- duygor-review-denied -->"
LABEL_OF = {command: label for label, command in process_issue.DECISIONS.items()}


def _is_id(value):
    return isinstance(value, int) and not isinstance(value, bool)


def labeler(number, label):
    """(id, type) of the account whose addition of the label still stands; (None, None) when none does."""
    out = process_issue.gh("api", "--paginate", "repos/%s/issues/%s/events?per_page=100" % (process_issue.repo(), number), "--jq", ".[] | [.event, .label.name, .actor.id, .actor.type] | tojson").stdout
    found = (None, None)
    for line in out.splitlines():
        if not line.strip():
            continue
        event, name, actor_id, actor_type = json.loads(line)
        if event == "labeled" and name == label:
            found = (actor_id, actor_type)
        elif event in ("closed", "reopened") or (event == "unlabeled" and name == label):
            found = (None, None)
    return found


def is_maintainer(command, account_id):
    """-> (maintainer, denial comment) as the CLI decides it from config.json."""
    result = json.loads(process_issue.cli("check-sender", "--sender-id", str(account_id), "--decision", command).stdout)
    return result.get("maintainer") is True, process_issue.comment_of(result)


def review(issue, command, sender_id=None, sender_type=None):
    """0 when handled or not ours, 1 when it failed (the issue stays open and the sweep retries it).

    sender_id/sender_type are the event's sender on the event path; the sweep passes none.
    """
    number = issue.get("number")
    label = LABEL_OF[command]
    labels = process_issue.open_human_labels(issue)
    if labels is None or process_issue.SUBMIT_LABEL not in labels or label not in labels or sender_type == "Bot":
        sys.stderr.write("issue %s: review skipped\n" % number)
        return 0
    try:
        actor_id, actor_type = labeler(number, label)
        if actor_type == "Bot" or not _is_id(actor_id) or (sender_id is not None and actor_id != sender_id):
            sys.stderr.write("issue %s: no standing %s label from this sender\n" % (number, label))
            return 0
        maintainer, denial = is_maintainer(command, actor_id)
        if not maintainer:
            process_issue.remove_label(number, label)
            process_issue.comment_once(number, DENIED_MARKER, denial)
            return 0
        if process_issue.decision_for(issue) != command:
            sys.stderr.write("issue %s: not awaiting approval or has both decision labels, left as it is\n" % number)
            return 0
        result = process_issue.run_cli(command, issue, "--sender-id", str(actor_id))
        if result.get("authorized") is not True:
            raise RuntimeError("the CLI did not accept the maintainer")
        comment = process_issue.comment_of(result)
        process_issue.push_registry()
        process_issue.finish(number, comment, labels)
        process_issue.finish_related(result)
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
    if command is None or not _is_id(number) or not _is_id(sender.get("id")):
        sys.stderr.write("not a decision label event\n")
        return 0
    process_issue.ensure_labels()
    # The live issue, so a label removed or an issue closed since the event is not acted on.
    issue = json.loads(process_issue.gh("api", "repos/%s/issues/%s" % (process_issue.repo(), number)).stdout)
    return review(issue, command, sender["id"], sender.get("type"))


if __name__ == "__main__":
    sys.exit(main())
