"""Processes one registry issue; shared by the submit workflow and the sweep.

Usage:
  process_issue.py <issue.json>   issue as returned by the GitHub issues API
  process_issue.py fail <number>  post the generic registry-error comment (once per issue)

Needs GH_TOKEN, GITHUB_TOKEN and GITHUB_REPOSITORY. Issue text is only ever data: it goes to the
CLI through a JSON file and to `gh` through body files, never through a shell.
Flow: run the CLI, rebuild the index, commit and push, then comment with the result and close
the issue. Any failure leaves the issue open (the sweep retries it) after one generic comment.
An unapproved author's first submission is the exception: it is labelled PENDING_LABEL (before the
comment, which is posted once per pending version) and stays open until a maintainer adds one of
DECISIONS (review_issue.py); the sweep never resubmits it.
"""

import hashlib
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
# Every label the workflows use, created (or corrected) at the start of each job: name -> (color, description).
LABELS = {
    "eklenti-gonder": ("0e8a16", "Eklenti gönderme formu / Add-on submission form"),
    "eklenti-kaldir": ("b60205", "Eklenti kaldırma formu / Add-on removal form"),
    PENDING_LABEL: ("fbca04", "Depo yöneticisinin onayını bekliyor / Awaiting maintainer approval"),
    "onaylandi": ("1d76db", "Yazar onaylandı (yalnız depo yöneticisi) / Author approved (maintainers only)"),
    "reddedildi": ("5319e7", "İlk gönderim reddedildi (yalnız depo yöneticisi) / First submission rejected (maintainers only)"),
}
ERROR_MARKER = "<!-- duygor-registry-error -->"
PENDING_MARKER = "<!-- duygor-pending:%s:%s -->"
RESULT_MARKER = "<!-- duygor-result:%s -->"
HERE = os.path.dirname(os.path.abspath(__file__))
ERROR_MESSAGE = os.path.join(HERE, "..", "messages", "registry-error.md")


def gh(*args, check=True):
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=check)


def repo():
    return os.environ["GITHUB_REPOSITORY"]


def ensure_labels():
    """Creates every label the workflows add, so adding one can never fail on a missing label."""
    for name, (color, description) in LABELS.items():
        gh("label", "create", name, "--repo", repo(), "--color", color, "--description", description, "--force")


def remove_label(number, label):
    """Removes the label from the issue; one that is already gone is fine."""
    result = gh("api", "--method", "DELETE", "repos/%s/issues/%s/labels/%s" % (repo(), number, label), check=False)
    if result.returncode != 0 and "404" not in result.stderr:
        raise RuntimeError("could not remove the label %s: %s" % (label, result.stderr.strip()))


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

    A submission awaiting approval is never handled as a new one. A decision label alone does not stop
    a submission that was not processed yet: once pending, the label is reviewed.
    """
    labels = open_human_labels(issue)
    if labels is None or PENDING_LABEL in labels:
        return None
    commands = {command for label, command in COMMANDS.items() if label in labels}
    return commands.pop() if len(commands) == 1 else None


def decisions_on(issue):
    """approve/reject for every decision label on an open, human-opened submit issue awaiting approval."""
    labels = open_human_labels(issue)
    if labels is None or SUBMIT_LABEL not in labels or PENDING_LABEL not in labels or "eklenti-kaldir" in labels:
        return []
    return [command for label, command in DECISIONS.items() if label in labels]


def decision_for(issue):
    """The one decision of an issue awaiting approval; None without one or with both."""
    decisions = decisions_on(issue)
    return decisions[0] if len(decisions) == 1 else None


def action_for(issue):
    """What the sweep does with an issue: approve/reject, register/remove, or None (skip)."""
    return decision_for(issue) or command_for(issue)


def live_issue(number):
    return json.loads(gh("api", "repos/%s/issues/%s" % (repo(), number)).stdout)


def comment_from_file(number, text):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as f:
        f.write(text)
    try:
        gh("issue", "comment", str(number), "--repo", repo(), "--body-file", f.name)
    finally:
        os.unlink(f.name)


def comment_once(number, marker, text):
    """Posts marker + text unless a comment on the issue already carries the marker."""
    existing = gh("api", "--paginate", "repos/%s/issues/%s/comments" % (repo(), number), "--jq", ".[].body").stdout
    if marker not in existing:
        comment_from_file(number, marker + "\n" + text)


def finish(number, comment, labels=None):
    """Answers an issue and closes it, dropping the approval labels so a reopened issue never replays a decision.

    labels are the issue's known label names; when unknown (None) every approval label is removed.
    The comment carries a marker of its own text, so a retry after a partial failure does not repeat it.
    """
    comment_once(number, RESULT_MARKER % hashlib.sha256(comment.encode("utf-8")).hexdigest()[:16], comment)
    for label in (PENDING_LABEL, *DECISIONS):
        if labels is None or label in labels:
            remove_label(number, label)
    gh("issue", "close", str(number), "--repo", repo())


def finish_related(result):
    """Answers and closes the other open issues the CLI result names (e.g. every issue of a published record)."""
    for item in result.get("related") or []:
        if isinstance(item, dict) and isinstance(item.get("number"), int) and isinstance(item.get("comment"), str):
            issue = live_issue(item["number"])
            # Already closed (by its author, or earlier in this run): nothing to answer.
            if issue.get("state") == "open":
                finish(item["number"], defuse_body(item["comment"]), {label.get("name") for label in issue.get("labels") or []})


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
    number = issue["number"]
    if command == "register" and result.get("pending") is True:
        # The label goes first: it keeps the sweep from resubmitting the issue, so the comment cannot repeat.
        gh("issue", "edit", str(number), "--repo", repo(), "--add-label", PENDING_LABEL)
        comment_once(number, PENDING_MARKER % (result.get("addon_id"), result.get("version")), comment)
        return
    finish(number, comment, open_human_labels(issue) or set())
    finish_related(result)


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
    ensure_labels()
    with open(argv[1], encoding="utf-8") as f:
        return process(json.load(f))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
