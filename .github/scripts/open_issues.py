"""Opens the issues `cli scan` printed; the CLI itself never opens any.

Usage: open_issues.py <scan.json>. Needs GH_TOKEN and GITHUB_REPOSITORY. Title and body go to
`gh` as one `--title=` argument and a body file, never through a shell. Every '@' is defused
except the owner mention the registry puts on the first line of a body.
"""

import json
import os
import re
import subprocess
import sys
import tempfile

_LEADING_MENTION = re.compile(r"@[A-Za-z0-9-]{1,39}\n\n")
_ZERO_WIDTH_SPACE = "​"


def defuse(text):
    return text.replace("@", "@" + _ZERO_WIDTH_SPACE)


def defuse_body(body):
    match = _LEADING_MENTION.match(body)
    if not match:
        return defuse(body)
    return match.group(0) + defuse(body[match.end():])


def main(scan_path):
    with open(scan_path, encoding="utf-8") as f:
        issues = json.load(f)
    if not isinstance(issues, list):
        sys.stderr.write("scan output must be a JSON list\n")
        return 1
    failed = 0
    for issue in issues:
        title = defuse(str(issue.get("title", "")).replace("\n", " "))
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as body:
            body.write(defuse_body(str(issue.get("body", ""))))
        try:
            result = subprocess.run(["gh", "issue", "create", "--repo", os.environ["GITHUB_REPOSITORY"], "--title=" + title, "--body-file", body.name])
            failed += result.returncode != 0
        finally:
            os.unlink(body.name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
