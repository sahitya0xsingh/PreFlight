"""The org profile: what ``preflight learn`` writes and ``preflight check`` reads."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SCHEMA = 1


def home() -> Path:
    return Path(os.environ.get("PREFLIGHT_HOME", Path.home() / ".preflight"))


def profile_path(owner: str) -> Path:
    return home() / "profiles" / f"{owner.lower()}.json"


def save(profile: dict[str, Any], path: Path | None = None) -> Path:
    path = path or profile_path(profile["org"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, indent=2, sort_keys=True))
    return path


def load(owner: str) -> dict[str, Any] | None:
    path = profile_path(owner)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return data if data.get("schema") == SCHEMA else None


def failure_count(profile: dict[str, Any] | None, repo: str, workflow_path: str, job_id: str) -> int:
    """How often this job failed on recent PRs, from the profile (0 if unknown).

    ``learn`` keys failures as ``<workflow file>::<job id>``.
    """
    if not profile:
        return 0
    entry = profile.get("repos", {}).get(repo.split("/")[-1].lower())
    if not entry:
        return 0
    rel = workflow_path.split(".github/workflows/")[-1]
    failures = entry.get("failures", {})
    return int(failures.get(f"{rel}::{job_id}", 0))
