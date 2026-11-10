"""GitHub API access through the ``gh`` CLI, so PreFlight never handles tokens."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from typing import Any


class GitHubError(Exception):
    pass


class NotFound(GitHubError):
    pass


def available() -> bool:
    return shutil.which("gh") is not None


def api(path: str, raw: bool = False) -> Any:
    cmd = ["gh", "api", "-H", "X-GitHub-Api-Version: 2022-11-28"]
    if raw:
        cmd += ["-H", "Accept: application/vnd.github.raw"]
    cmd.append(path)
    for attempt in range(4):
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            break
        msg = (proc.stderr or proc.stdout).strip()
        if "404" in msg or "Not Found" in msg:
            raise NotFound(f"{path}: not found")
        # GitHub returns 502/503/504 under load; those usually pass on retry.
        if not re.search(r"HTTP 50[234]", msg) or attempt == 3:
            raise GitHubError(f"gh api {path}: {msg}")
        time.sleep(2 ** attempt)
    if raw:
        return proc.stdout
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def paginate(path: str, key: str | None = None, limit: int | None = None) -> list[Any]:
    """Fetch every page of a list endpoint (``key`` for wrapped lists like runs)."""
    items: list[Any] = []
    page = 1
    sep = "&" if "?" in path else "?"
    while True:
        data = api(f"{path}{sep}per_page=100&page={page}")
        batch = data.get(key, []) if key else data
        items.extend(batch)
        if len(batch) < 100 or (limit and len(items) >= limit):
            break
        page += 1
    return items[:limit] if limit else items


def owner_repos(owner: str) -> list[dict[str, Any]]:
    """Repos of an org, falling back to a user account."""
    try:
        return paginate(f"orgs/{owner}/repos?type=public")
    except NotFound:
        return paginate(f"users/{owner}/repos")
