"""Processes one registry issue; shared by the submit workflow and the sweep.

Usage:
  process_issue.py <issue.json>   issue as returned by the GitHub issues API
  process_issue.py fail <number>  post the generic registry-error comment (once per issue)

Needs GH_TOKEN, GITHUB_TOKEN and GITHUB_REPOSITORY. Issue text is only ever data: it goes to the
CLI through a JSON file and to `gh` through body files, never through a shell.
Flow: run the CLI, rebuild the index, commit and push, then comment with the result and close
the issue. Any failure leaves the issue open (the sweep retries it) after one generic comment.
"""

import json
import os
import subprocess
import sys
import tempfile

from text_safety import defuse_body

COMMANDS = {"eklenti-gonder": "register", "eklenti-kaldir": "remove"}
ERROR_MARKER = "<!-- duygor-registry-error -->"
HERE = os.path.dirname(os.path.abspath(__file__))
ERROR_MESSAGE = os.path.join(HERE, "..", "messages", "registry-error.md")


def gh(*args, check=True):
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=check)


def repo():
    return os.environ["GITHUB_REPOSITORY"]


def command_for(issue):
    """register/remove for an open, human-opened issue carrying exactly one form label; else None."""
    if "pull_request" in issue or issue.get("state") != "open":
        return None
    user = issue.get("user") or {}
    if user.get("type") == "Bot":
        return None
    labels = {label.get("name") for label in issue.get("labels") or []}
    commands = {command for label, command in COMMANDS.items() if label in labels}
    return commands.pop() if len(commands) == 1 else None


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


def handle(issue, command):
    user = issue["user"]
    payload = {"issue_number": issue["number"], "author_id": user["id"], "author_login": user["login"], "body": issue.get("body") or ""}
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "issue.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        result = json.loads(cli(command, "--issue-json", path).stdout)
    comment = result.get("comment")
    if not isinstance(comment, str) or not comment.strip():
        raise ValueError("the CLI result has no comment")
    comment = defuse_body(comment)
    cli("build-index", "--out", "site/index.json")
    subprocess.run(["bash", os.path.join(HERE, "commit_registry.sh")], check=True)
    comment_from_file(issue["number"], comment)
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
