"""GitHub Actions path-filter matching (``on.<event>.paths`` / ``paths-ignore``)."""

from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=None)
def _compile(pattern: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**", i):
                i += 2
                if i < len(pattern) and pattern[i] == "/":
                    out.append("(?:.*/)?")  # '**/' also matches zero directories
                    i += 1
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            # GitHub: '?' matches zero or one of the preceding character.
            if out:
                out[-1] = f"(?:{out[-1]})?"
            else:
                out.append(re.escape(c))
        elif c == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape(c))
            else:
                out.append(pattern[i : end + 1])
                i = end
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


def match(pattern: str, path: str) -> bool:
    return _compile(pattern).match(path) is not None


def _included(patterns: list[str], path: str) -> bool:
    """Apply patterns in order; a later ``!pattern`` can exclude an earlier match."""
    result = False
    for p in patterns:
        if p.startswith("!"):
            if result and match(p[1:], path):
                result = False
        elif match(p, path):
            result = True
    return result


def triggers(changed: list[str], paths: list[str] | None, paths_ignore: list[str] | None) -> bool:
    """Would a change to ``changed`` files trigger a workflow with these filters?

    No filters → always. ``paths`` → at least one file is included.
    ``paths-ignore`` → at least one file is not ignored.
    """
    if not changed:
        return paths is None and paths_ignore is None
    if paths:
        return any(_included(paths, f) for f in changed)
    if paths_ignore:
        return not all(_included(paths_ignore, f) for f in changed)
    return True
