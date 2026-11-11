"""Writes the CLI result's `comment` text to a file for `gh issue comment --body-file`.

Usage: comment_body.py <result.json> <comment.md>
"""

import json
import sys


def main(result_path, comment_path):
    with open(result_path, encoding="utf-8") as f:
        result = json.load(f)
    comment = result.get("comment")
    if not isinstance(comment, str) or not comment.strip():
        sys.stderr.write("the CLI result has no comment\n")
        return 1
    with open(comment_path, "w", encoding="utf-8") as f:
        f.write(comment)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
