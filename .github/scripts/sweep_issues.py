"""Processes every open, human-opened issue that carries a form label.

GitHub keeps one pending run per concurrency group, so a burst of submissions can lose runs;
the open form-labelled issues are the queue, and handled issues are closed. process_issue.action_for
picks what to do: a new form goes through process_issue.process exactly as in the submit workflow,
an issue awaiting approval (onay-bekliyor) is skipped unless it carries a decision label, and then
it goes through review_issue.review, which takes the account from the issue's events (only a label
addition that still stands) and checks it like the review workflow does. Exit 1 if any issue failed.
"""

import json
import sys

import process_issue
import review_issue


def open_issues(label):
    out = process_issue.gh("api", "--paginate", "repos/%s/issues?state=open&labels=%s&per_page=100" % (process_issue.repo(), label), "--jq", ".[] | tojson").stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def handle(issue):
    """0 when handled or skipped, 1 when it failed.

    On an issue awaiting approval every decision label present is reviewed, so a stray label from a
    non-maintainer (whose own run was lost) is removed and the maintainer's label is then applied
    instead of both staying stuck. A second pass applies the label that was blocked in the first.
    """
    decisions = process_issue.decisions_on(issue)
    if not decisions:
        action = process_issue.command_for(issue)
        if action is None:
            sys.stderr.write("issue %s skipped\n" % issue.get("number"))
            return 0
        return process_issue.process(issue)
    number = issue["number"]
    for command in decisions + [None]:
        # Each step reads the live copy: an earlier one (or a decision on another issue in this sweep) may have changed it.
        try:
            issue = process_issue.live_issue(number)
        except Exception as e:
            sys.stderr.write("issue %s: could not read it: %s\n" % (number, e))
            return 1
        command = command or process_issue.decision_for(issue)
        if command is None or command not in process_issue.decisions_on(issue):
            continue
        if review_issue.review(issue, command):
            return 1
    return 0


def main():
    process_issue.ensure_labels()
    found = {}
    for label in process_issue.COMMANDS:
        for issue in open_issues(label):
            found[issue["number"]] = issue
    failed = 0
    for number in sorted(found):
        failed += handle(found[number])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
