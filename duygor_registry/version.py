"""Numeric store versions: one to four dot-separated parts of one to nine ASCII digits.

Mirrors StoreVersion.java in the Android app; the two must stay identical.
"""

import re

# [0-9] instead of \d (which matches other Unicode digits) and fullmatch
# instead of $ (which tolerates a trailing newline).
_VALID = re.compile(r"[0-9]{1,9}(\.[0-9]{1,9}){0,3}")


def is_valid(v):
    return isinstance(v, str) and _VALID.fullmatch(v) is not None


def compare(a, b):
    """Part-wise numeric comparison, missing parts count as 0. Returns -1, 0 or 1."""
    if not is_valid(a) or not is_valid(b):
        raise ValueError("Invalid version: " + repr(b if is_valid(a) else a))
    pa = [int(x) for x in a.split(".")]
    pb = [int(x) for x in b.split(".")]
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa))
    pb += [0] * (n - len(pb))
    return (pa > pb) - (pa < pb)


def is_newer(candidate, installed):
    """True when candidate is valid and newer; an invalid installed version counts as older."""
    if not is_valid(candidate):
        return False
    if not is_valid(installed):
        return True
    return compare(candidate, installed) > 0
