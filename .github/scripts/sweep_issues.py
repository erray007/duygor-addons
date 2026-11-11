"""Processes every open, human-opened issue that carries a form label.

GitHub keeps one pending run per concurrency group, so a burst of submissions can lose runs;
the open form-labelled issues are the queue, and handled issues are closed. Each issue goes
through process_issue.process, exactly as in the submit workflow. Exit 1 if any issue failed.
"""

import json
import sys

import process_issue


def open_issues(label):
    out = process_issue.gh("api", "--paginate", "repos/%s/issues?state=open&labels=%s&per_page=100" % (process_issue.repo(), label), "--jq", ".[] | tojson").stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def main():
    found = {}
    for label in process_issue.COMMANDS:
        for issue in open_issues(label):
            found[issue["number"]] = issue
    failed = 0
    for number in sorted(found):
        failed += process_issue.process(found[number])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
