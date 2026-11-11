"""Download marketplace actions and read their ``action.yml``.

``uses: owner/repo[/path]@ref`` is fetched once from codeload.github.com (no
token needed for public actions) and kept in ``~/.preflight/actions``. A ref
that is a commit SHA never changes; tags and branches are refreshed after a
day, as tags can be moved.
"""

from __future__ import annotations

import io
import re
import shutil
import tarfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import profile

_SHA = re.compile(r"^[0-9a-f]{40}$")
_LOCK = threading.Lock()
REFRESH = 24 * 3600


class FetchError(RuntimeError):
    pass


@dataclass
class Action:
    uses: str                  # as written in the workflow
    owner: str
    repo: str
    path: str                  # subdirectory inside the repo ("" for the root)
    ref: str
    dir: Path                  # host directory holding action.yml
    meta: dict[str, Any] = field(default_factory=dict)
    local: bool = False        # uses: ./path, read from the checkout

    @property
    def using(self) -> str:
        return str((self.meta.get("runs") or {}).get("using") or "").lower()

    @property
    def kind(self) -> str:
        """node | composite | docker | unknown"""
        u = self.using
        if u.startswith("node"):
            return "node"
        if u in ("composite", "docker"):
            return u
        return "unknown"

    @property
    def runs(self) -> dict[str, Any]:
        return self.meta.get("runs") or {}

    @property
    def inputs(self) -> dict[str, Any]:
        return self.meta.get("inputs") or {}

    @property
    def outputs(self) -> dict[str, Any]:
        return self.meta.get("outputs") or {}

    @property
    def repo_slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def container_key(self) -> str:
        """Relative path under ``_actions`` inside the job container."""
        return f"{self.owner}/{self.repo}/{self.ref}"


def parse_uses(uses: str) -> tuple[str, str, str, str]:
    """``owner/repo/sub/dir@ref`` → (owner, repo, sub/dir, ref)."""
    if "@" not in uses:
        raise FetchError(f"{uses}: no @ref")
    name, ref = uses.split("@", 1)
    parts = name.strip().split("/")
    if len(parts) < 2:
        raise FetchError(f"{uses}: expected owner/repo@ref")
    return parts[0], parts[1], "/".join(parts[2:]), ref.strip()


def _read_meta(d: Path, what: str) -> dict[str, Any]:
    for name in ("action.yml", "action.yaml"):
        f = d / name
        if f.is_file():
            data = yaml.safe_load(f.read_text()) or {}
            if not isinstance(data, dict):
                raise FetchError(f"{what}: {name} is not a mapping")
            return data
    raise FetchError(f"{what}: no action.yml")


def _download(owner: str, repo: str, ref: str, dest: Path) -> None:
    url = f"https://codeload.github.com/{owner}/{repo}/tar.gz/{ref}"
    req = urllib.request.Request(url, headers={"User-Agent": "preflight-ci"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        raise FetchError(f"{owner}/{repo}@{ref}: download failed (HTTP {e.code})") from e
    except OSError as e:
        raise FetchError(f"{owner}/{repo}@{ref}: download failed ({e})") from e
    tmp = dest.with_name(dest.name + ".part")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = []
        for m in tar.getmembers():
            # Strip the "<repo>-<ref>/" top directory GitHub adds.
            parts = m.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            m.name = parts[1]
            members.append(m)
        tar.extractall(tmp, members=members, filter="data")
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    (dest / ".preflight-fetched").write_text(str(time.time()))


def fetch(uses: str) -> Action:
    owner, repo, sub, ref = parse_uses(uses)
    base = profile.home() / "actions" / owner.lower() / repo.lower() / re.sub(r"[^\w.-]", "_", ref)
    with _LOCK:
        stamp = base / ".preflight-fetched"
        stale = not stamp.exists() or (
            not _SHA.match(ref) and time.time() - float(stamp.read_text() or 0) > REFRESH)
        if stale:
            base.parent.mkdir(parents=True, exist_ok=True)
            _download(owner, repo, ref, base)
    d = base / sub if sub else base
    return Action(uses=uses, owner=owner, repo=repo, path=sub, ref=ref, dir=d, meta=_read_meta(d, uses))


def local(uses: str, root: Path) -> Action:
    """``uses: ./path`` — an action stored in the repo itself."""
    rel = uses[2:].strip("/")
    d = root / rel
    return Action(uses=uses, owner="", repo="", path=rel, ref="", dir=d, meta=_read_meta(d, uses), local=True)


def resolve(uses: str, root: Path) -> Action:
    return local(uses, root) if uses.startswith("./") else fetch(uses)
