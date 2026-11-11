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
    """0 when handled or skipped, 1 when it failed."""
    action = process_issue.action_for(issue)
    if action is None:
        sys.stderr.write("issue %s skipped\n" % issue.get("number"))
        return 0
    if action in process_issue.COMMANDS.values():
        return process_issue.process(issue)
    # A decision earlier in this sweep may have closed it (related issues), so the live copy decides.
    try:
        issue = json.loads(process_issue.gh("api", "repos/%s/issues/%s" % (process_issue.repo(), issue["number"])).stdout)
    except Exception as e:
        sys.stderr.write("issue %s: could not read it: %s\n" % (issue.get("number"), e))
        return 1
    action = process_issue.decision_for(issue)
    return review_issue.review(issue, action) if action else 0


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
