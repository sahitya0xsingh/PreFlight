"""How PreFlight treats marketplace actions when it runs a job on this machine.

Each known action gets a handler kind:

- ``noop``: nothing to do locally (the repo is already checked out, caches
  live on disk, artifacts stay where they are).
- ``tool``: the action installs a tool; locally we only check the tool exists.
- ``skip``: the action talks to GitHub or another service; skipping it does
  not change whether the code passes.

Anything else is ``unknown``: the step is skipped and the job is reported as
partly covered.
"""

from __future__ import annotations

KNOWN: dict[str, tuple[str, str]] = {
    # action (owner/name, lowercase): (kind, detail)
    "actions/checkout": ("noop", "repo is already checked out"),
    "actions/cache": ("noop", "local caches are reused"),
    "actions/cache/restore": ("noop", "local caches are reused"),
    "actions/cache/save": ("noop", "local caches are reused"),
    "actions/upload-artifact": ("noop", "artifacts stay on disk"),
    "actions/download-artifact": ("noop", "artifacts stay on disk"),
    "actions/setup-java": ("tool", "java"),
    "actions/setup-node": ("tool", "node"),
    "actions/setup-python": ("tool", "python3"),
    "actions/setup-go": ("tool", "go"),
    "actions/setup-dotnet": ("tool", "dotnet"),
    "ruby/setup-ruby": ("tool", "ruby"),
    "gradle/actions/setup-gradle": ("noop", "gradle wrapper used directly"),
    "gradle/gradle-build-action": ("noop", "gradle wrapper used directly"),
    "pnpm/action-setup": ("tool", "pnpm"),
    "astral-sh/setup-uv": ("tool", "uv"),
    "actions/github-script": ("skip", "calls the GitHub API"),
    "actions/labeler": ("skip", "labels PRs"),
    "actions/stale": ("skip", "bot"),
    "github/codeql-action/upload-sarif": ("skip", "uploads results to GitHub"),
    "codecov/codecov-action": ("skip", "uploads coverage"),
    "peter-evans/create-or-update-comment": ("skip", "comments on the PR"),
    "marocchino/sticky-pull-request-comment": ("skip", "comments on the PR"),
}


def action_name(uses: str) -> str:
    """``actions/setup-java@v4`` → ``actions/setup-java``."""
    return uses.split("@", 1)[0].strip().lower()


def classify(uses: str) -> tuple[str, str]:
    if uses.startswith("./"):
        return ("unknown", "local action in this repo (phase 2)")
    if uses.startswith("docker://"):
        return ("unknown", "docker action (phase 2)")
    name = action_name(uses)
    if name in KNOWN:
        return KNOWN[name]
    return ("unknown", "not emulated yet")
