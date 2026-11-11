"""Defuses @mentions in text the workflows post to GitHub."""

import re

_LEADING_MENTION = re.compile(r"@[A-Za-z0-9-]{1,39}\n\n")
_ZERO_WIDTH_SPACE = "\u200b"


def defuse(text):
    return text.replace("@", "@" + _ZERO_WIDTH_SPACE)


def defuse_body(body):
    match = _LEADING_MENTION.match(body)
    if not match:
        return defuse(body)
    return match.group(0) + defuse(body[match.end():])
