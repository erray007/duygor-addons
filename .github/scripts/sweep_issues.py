"""Processes every open, human-opened issue that carries a form label.

GitHub keeps one pending run per concurrency group, so a burst of submissions can lose runs;
the open form-labelled issues are the queue, and handled issues are closed. process_issue.action_for
picks what to do: a new form goes through process_issue.process exactly as in the submit workflow,
an issue awaiting approval (onay-bekliyor) is skipped, and one carrying a decision label whose
review run was lost goes through review_issue.review with the account that last added that label,
read from the issue's events. Exit 1 if any issue failed.
"""

import json
import sys

import process_issue
import review_issue


def open_issues(label):
    out = process_issue.gh("api", "--paginate", "repos/%s/issues?state=open&labels=%s&per_page=100" % (process_issue.repo(), label), "--jq", ".[] | tojson").stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def labeler(number, label):
    """(id, type) of the account that last added the label to the issue; (None, None) when none did."""
    out = process_issue.gh("api", "--paginate", "repos/%s/issues/%s/events?per_page=100" % (process_issue.repo(), number), "--jq", '.[] | select(.event == "labeled") | [.label.name, .actor.id, .actor.type] | tojson').stdout
    found = (None, None)
    for line in out.splitlines():
        if line.strip():
            name, actor_id, actor_type = json.loads(line)
            if name == label:
                found = (actor_id, actor_type)
    return found


def handle(issue):
    """0 when handled or skipped, 1 when it failed."""
    action = process_issue.action_for(issue)
    if action is None:
        sys.stderr.write("issue %s skipped\n" % issue.get("number"))
        return 0
    if action in process_issue.COMMANDS.values():
        return process_issue.process(issue)
    try:
        actor_id, actor_type = labeler(issue["number"], review_issue.LABEL_OF[action])
    except Exception as e:
        sys.stderr.write("issue %s: could not read who added the label: %s\n" % (issue.get("number"), e))
        return 1
    return review_issue.review(issue, action, actor_id, actor_type)


def main():
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
