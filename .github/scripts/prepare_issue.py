"""Turns the issue event into the CLI's input without ever putting issue text in a shell.

Reads $GITHUB_EVENT_PATH, writes {issue_number, author_id, author_login, body} to the path
given as argv[1], and sets the step outputs `command` (register or remove) and `number`.
"""

import json
import os
import sys

COMMANDS = {"eklenti-gonder": "register", "eklenti-kaldir": "remove"}


def main(out_path):
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
        issue = json.load(f)["issue"]
    labels = {label.get("name") for label in issue.get("labels") or []}
    commands = sorted({command for label, command in COMMANDS.items() if label in labels})
    if len(commands) != 1:
        sys.stderr.write("expected exactly one form label, found: %s\n" % sorted(labels))
        return 1
    user = issue["user"]
    if user.get("type") == "Bot":
        sys.stderr.write("issues opened by bots are ignored\n")
        return 1
    payload = {"issue_number": issue["number"], "author_id": user["id"], "author_login": user["login"], "body": issue.get("body") or ""}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
        f.write("command=%s\nnumber=%d\n" % (commands[0], issue["number"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
