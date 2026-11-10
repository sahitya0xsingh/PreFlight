"""Read what PreFlight needs from the local git checkout."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_SLUG_RE = re.compile(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$")


class GitError(Exception):
    pass


def git(root: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def repo_root(start: Path) -> Path:
    return Path(git(start, "rev-parse", "--show-toplevel"))


def slug_from_url(url: str) -> str | None:
    m = _SLUG_RE.search(url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def remotes(root: Path) -> dict[str, str]:
    """Remote name → ``owner/repo`` for GitHub remotes."""
    out: dict[str, str] = {}
    for line in git(root, "remote", "-v").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            slug = slug_from_url(parts[1])
            if slug:
                out.setdefault(parts[0], slug)
    return out


def remote_default_branch(root: Path, remote: str) -> str | None:
    ref = git(root, "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD", check=False)
    return ref.rsplit("/", 1)[-1] if ref else None


def ref_exists(root: Path, ref: str) -> bool:
    return bool(git(root, "rev-parse", "--verify", "--quiet", ref, check=False))


def current_branch(root: Path) -> str:
    return git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False) or "HEAD"


def head_sha(root: Path) -> str:
    return git(root, "rev-parse", "HEAD", check=False)


def changed_files(root: Path, base: str, include_worktree: bool) -> list[str]:
    """Files changed since the merge base with ``base``."""
    merge_base = git(root, "merge-base", base, "HEAD")
    files = set(git(root, "diff", "--name-only", f"{merge_base}..HEAD").splitlines())
    if include_worktree:
        files |= set(git(root, "diff", "--name-only", "HEAD").splitlines())
        files |= set(git(root, "ls-files", "--others", "--exclude-standard").splitlines())
    return sorted(f for f in files if f)
