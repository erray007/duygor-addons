"""Processes one registry issue; shared by the submit workflow and the sweep.

Usage:
  process_issue.py <issue.json>   issue as returned by the GitHub issues API
  process_issue.py fail <number>  post the generic registry-error comment (once per issue)

Needs GH_TOKEN, GITHUB_TOKEN and GITHUB_REPOSITORY. Issue text is only ever data: it goes to the
CLI through a JSON file and to `gh` through body files, never through a shell.
Flow: run the CLI, rebuild the index, commit and push, then comment with the result and close
the issue. Any failure leaves the issue open (the sweep retries it) after one generic comment.
An unapproved author's first submission is the exception: it is labelled PENDING_LABEL and stays
open until a maintainer adds one of DECISIONS (review_issue.py); the sweep never resubmits it.
"""

import json
import os
import subprocess
import sys
import tempfile

from text_safety import defuse_body

COMMANDS = {"eklenti-gonder": "register", "eklenti-kaldir": "remove"}
SUBMIT_LABEL = "eklenti-gonder"
PENDING_LABEL = "onay-bekliyor"
DECISIONS = {"onaylandi": "approve", "reddedildi": "reject"}
ERROR_MARKER = "<!-- duygor-registry-error -->"
HERE = os.path.dirname(os.path.abspath(__file__))
ERROR_MESSAGE = os.path.join(HERE, "..", "messages", "registry-error.md")


def gh(*args, check=True):
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=check)


def repo():
    return os.environ["GITHUB_REPOSITORY"]


def open_human_labels(issue):
    """The label names of an open, human-opened issue (not a pull request); None for anything else."""
    if "pull_request" in issue or issue.get("state") != "open":
        return None
    user = issue.get("user") or {}
    if user.get("type") == "Bot":
        return None
    return {label.get("name") for label in issue.get("labels") or []}


def command_for(issue):
    """register/remove for an open, human-opened issue carrying exactly one form label; else None.

    A submission awaiting approval, or one a maintainer has decided on, is never handled as a new one.
    """
    labels = open_human_labels(issue)
    if labels is None or PENDING_LABEL in labels or labels & DECISIONS.keys():
        return None
    commands = {command for label, command in COMMANDS.items() if label in labels}
    return commands.pop() if len(commands) == 1 else None


def decision_for(issue):
    """approve/reject for an open, human-opened submit issue carrying exactly one decision label; else None."""
    labels = open_human_labels(issue)
    if labels is None or SUBMIT_LABEL not in labels or "eklenti-kaldir" in labels:
        return None
    decisions = [command for label, command in DECISIONS.items() if label in labels]
    return decisions[0] if len(decisions) == 1 else None


def action_for(issue):
    """What the sweep does with an issue: approve/reject, register/remove, or None (skip)."""
    return decision_for(issue) or command_for(issue)


def comment_from_file(number, text):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as f:
        f.write(text)
    try:
        gh("issue", "comment", str(number), "--repo", repo(), "--body-file", f.name)
    finally:
        os.unlink(f.name)


def post_failure(number):
    """The generic bilingual error comment, at most once per issue; never raises."""
    try:
        existing = gh("api", "--paginate", "repos/%s/issues/%s/comments" % (repo(), number), "--jq", ".[].body").stdout
        if ERROR_MARKER in existing:
            return
        with open(ERROR_MESSAGE, encoding="utf-8") as f:
            comment_from_file(number, f.read())
    except Exception as e:
        sys.stderr.write("could not post the error comment: %s\n" % e)


def reset_tree():
    """Drop uncommitted or unpushed registry state so one failure cannot leak into the next issue."""
    subprocess.run(["git", "fetch", "origin", "main"])
    subprocess.run(["git", "reset", "--hard", "origin/main"])
    subprocess.run(["git", "clean", "-fd", "addons", "site"])


def cli(*args):
    return subprocess.run([sys.executable, "-m", "duygor_registry.cli", *args], capture_output=True, text=True, check=True)


def run_cli(command, issue, *extra):
    """Runs a CLI command on the issue (passed as a JSON file) and returns its parsed result."""
    user = issue["user"]
    payload = {"issue_number": issue["number"], "author_id": user["id"], "author_login": user["login"], "body": issue.get("body") or ""}
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "issue.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return json.loads(cli(command, "--issue-json", path, *extra).stdout)


def comment_of(result):
    comment = result.get("comment")
    if not isinstance(comment, str) or not comment.strip():
        raise ValueError("the CLI result has no comment")
    return defuse_body(comment)


def push_registry():
    cli("build-index", "--out", "site/index.json")
    subprocess.run(["bash", os.path.join(HERE, "commit_registry.sh")], check=True)


def handle(issue, command):
    result = run_cli(command, issue)
    comment = comment_of(result)
    push_registry()
    comment_from_file(issue["number"], comment)
    if command == "register" and result.get("pending") is True:
        # Left open for the maintainer; the label also keeps the sweep from resubmitting it.
        gh("issue", "edit", str(issue["number"]), "--repo", repo(), "--add-label", PENDING_LABEL)
    else:
        gh("issue", "close", str(issue["number"]), "--repo", repo())


def process(issue):
    """0 when handled or not ours, 1 when it failed (issue stays open)."""
    command = command_for(issue)
    if command is None:
        sys.stderr.write("issue %s skipped\n" % issue.get("number"))
        return 0
    try:
        handle(issue, command)
        return 0
    except Exception as e:
        sys.stderr.write("issue %s failed: %s\n" % (issue.get("number"), e))
        reset_tree()
        post_failure(issue["number"])
        return 1


def main(argv):
    if len(argv) == 3 and argv[1] == "fail":
        post_failure(int(argv[2]))
        return 0
    with open(argv[1], encoding="utf-8") as f:
        return process(json.load(f))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
