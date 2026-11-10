"""``preflight learn <org>``: scan an org's repos and write its profile."""

from __future__ import annotations

import platform
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from . import github, profile, workflow

# Root-level files and folders that mean another CI system is in use.
OTHER_CI = {
    "azure-pipelines.yml": "Azure Pipelines",
    ".azure-pipelines": "Azure Pipelines",
    ".circleci": "CircleCI",
    ".gitlab-ci.yml": "GitLab CI",
    "Jenkinsfile": "Jenkins",
    ".travis.yml": "Travis CI",
    ".semaphore": "Semaphore",
    "appveyor.yml": "AppVeyor",
    ".appveyor.yml": "AppVeyor",
    ".cirrus.yml": "Cirrus CI",
    "bitbucket-pipelines.yml": "Bitbucket Pipelines",
    ".drone.yml": "Drone",
    ".buildkite": "Buildkite",
}

_MATRIX_SUFFIX = re.compile(r"\s*\(.*\)\s*$")


def host_os() -> str:
    return {"Darwin": "macOS", "Windows": "Windows"}.get(platform.system(), "Linux")


def _job_key(wf: workflow.Workflow, api_job_name: str) -> str:
    """Map a job name from the API (``check (lock-tainting)``) to a job id."""
    base = _MATRIX_SUFFIX.sub("", api_job_name).strip()
    for job in wf.jobs:
        if base in (job.id, job.name) or api_job_name in (job.id, job.name):
            return job.id
    return base


def learn_repo(owner: str, repo: dict[str, Any], sample: int, log) -> dict[str, Any]:
    name = repo["name"]
    full = f"{owner}/{name}"
    branch = repo.get("default_branch") or "main"
    entry: dict[str, Any] = {
        "default_branch": branch,
        "other_ci": [],
        "workflows": [],
        "failures": {},
        "failed_pr_runs_sampled": 0,
    }
    try:
        tree = github.api(f"repos/{full}/git/trees/{branch}")
        names = {t["path"] for t in tree.get("tree", [])}
        entry["other_ci"] = sorted({v for k, v in OTHER_CI.items() if k in names})
    except github.GitHubError:
        pass

    parsed: dict[str, workflow.Workflow] = {}
    try:
        listing = github.api(f"repos/{full}/contents/.github/workflows?ref={branch}")
    except github.NotFound:
        listing = []
    for f in listing or []:
        if not f["name"].endswith((".yml", ".yaml")):
            continue
        try:
            text = github.api(f"repos/{full}/contents/{f['path']}?ref={branch}", raw=True)
            wf = workflow.parse(text, f["path"])
        except Exception as e:  # keep going: one bad file should not drop the repo
            log(f"  {full}: skipped {f['path']}: {e}")
            continue
        parsed[f["path"].split(".github/workflows/")[-1]] = wf
        paths, ignore = wf.pr_filters()
        jobs = []
        for job in wf.jobs:
            workflow.classify(job, host_os())
            jobs.append({
                "id": job.id,
                "name": job.name,
                "runs_on": job.runs_on if isinstance(job.runs_on, (str, list)) else str(job.runs_on),
                "verdict": job.verdict,
                "reasons": job.reasons,
                "matrix_size": len(workflow.expand_matrix(job.matrix)) if not isinstance(job.matrix, str) else None,
            })
        entry["workflows"].append({
            "path": f["path"],
            "sha": f.get("sha"),
            "name": wf.name,
            "pr_event": wf.pr_event,
            "paths": paths,
            "paths_ignore": ignore,
            "jobs": jobs,
        })

    # Failure history: which jobs failed on recent PR runs.
    if sample and parsed:
        try:
            runs = github.paginate(
                f"repos/{full}/actions/runs?event=pull_request&status=failure", key="workflow_runs", limit=sample
            )
        except github.GitHubError:
            runs = []
        counts: Counter[str] = Counter()
        for run in runs:
            rel = (run.get("path") or "").split(".github/workflows/")[-1].split("@")[0]
            wf = parsed.get(rel)
            try:
                jobs = github.api(f"repos/{full}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100")
            except github.GitHubError:
                continue
            for j in jobs.get("jobs", []):
                if j.get("conclusion") == "failure":
                    key = _job_key(wf, j["name"]) if wf else _MATRIX_SUFFIX.sub("", j["name"])
                    counts[f"{rel}::{key}"] += 1
        entry["failures"] = dict(counts.most_common())
        entry["failed_pr_runs_sampled"] = len(runs)
    return entry


def learn(owner: str, only: list[str] | None, sample: int, include_forks: bool, log=print) -> dict[str, Any]:
    if not github.available():
        raise github.GitHubError("the GitHub CLI (gh) is not installed; install it and run `gh auth login`")
    repos = github.owner_repos(owner)
    repos = [
        r for r in repos
        if not r.get("archived") and (include_forks or not r.get("fork"))
        and (not only or r["name"].lower() in {o.lower() for o in only})
    ]
    log(f"Scanning {len(repos)} repos in {owner}...")
    result: dict[str, Any] = {
        "schema": profile.SCHEMA,
        "org": owner,
        "learned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "learned_on": host_os(),
        "repos": {},
    }

    def one(repo):
        try:
            return repo["name"], learn_repo(owner, repo, sample, log)
        except github.GitHubError as e:
            log(f"  {repo['name']}: {e}")
            return repo["name"], None

    with ThreadPoolExecutor(max_workers=6) as pool:
        for name, entry in pool.map(one, repos):
            if entry is not None:
                result["repos"][name.lower()] = entry
                n_pr = sum(1 for w in entry["workflows"] if w["pr_event"])
                log(f"  {name}: {len(entry['workflows'])} workflows, {n_pr} run on PRs")
    return result


def summarize(prof: dict[str, Any]) -> dict[str, Any]:
    """Org-wide numbers for the end of ``learn`` and for ``preflight show``."""
    verdicts: Counter[str] = Counter()
    other: Counter[str] = Counter()
    failures: list[tuple[int, str]] = []
    for name, entry in prof["repos"].items():
        for ci in entry["other_ci"]:
            other[ci] += 1
        for wf in entry["workflows"]:
            if not wf["pr_event"]:
                continue
            for job in wf["jobs"]:
                verdicts[job["verdict"]] += 1
        for key, n in entry["failures"].items():
            failures.append((n, f"{name}: {key}"))
    code_jobs = sum(n for v, n in verdicts.items() if v != workflow.NO_CODE)
    runnable = sum(verdicts[v] for v in workflow.RUNNABLE)
    return {
        "repos": len(prof["repos"]),
        "pr_jobs": sum(verdicts.values()),
        "verdicts": dict(verdicts),
        "coverage": (runnable / code_jobs) if code_jobs else None,
        "other_ci": dict(other),
        "top_failures": sorted(failures, reverse=True)[:10],
    }
