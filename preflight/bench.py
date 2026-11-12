"""``preflight bench``: measure how often a local run agrees with GitHub.

For recent PRs whose CI finished on GitHub, rebuild the commit GitHub tested
(the PR merged into the base branch as it was then), run the jobs PreFlight
can run, and compare each job's pass/fail with GitHub's result for that job.

Everything lives in ``~/.preflight/bench/<owner>_<repo>/``: a clone of the
repo (your own checkout is never touched), ``cases.json`` and
``results.jsonl``. Results are appended per case, so a run that stops can be
resumed and ``preflight bench report`` works at any time.
"""

from __future__ import annotations

import calendar
import json
import os
import random
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import check, expr, github, gitinfo, profile
from .runner import job_label

CONCLUSIVE = ("success", "failure")
PR_EVENTS = ("pull_request", "pull_request_target")


@dataclass
class GhJob:
    workflow: str        # workflow file name, e.g. checker.yml
    name: str            # job name as GitHub shows it
    conclusion: str
    seconds: float
    url: str


@dataclass
class Case:
    sha: str             # PR head commit
    title: str
    created_at: str      # first CI run for this commit
    failed: bool         # any of its workflow runs failed
    workflows: list[str] = field(default_factory=list)   # workflow files GitHub ran
    jobs: list[GhJob] = field(default_factory=list)


def bench_dir(repo: str) -> Path:
    return profile.home() / "bench" / repo.replace("/", "_")


# ---------------------------------------------------------------- sampling

def _runs(repo: str, pages: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in PR_EVENTS:
        for page in range(1, pages + 1):
            data = github.api(f"repos/{repo}/actions/runs?event={event}&status=completed&per_page=100&page={page}")
            batch = data.get("workflow_runs", [])
            out += batch
            if len(batch) < 100:
                break
    return out


def sample_cases(repo: str, count: int, failing_share: float, pages: int = 5, skip: set[str] | None = None,
                 min_age_hours: float = 2.0) -> list[Case]:
    """Recent PR commits whose CI is complete: ``failing_share`` of them with a failed run."""
    skip = skip or set()
    by_sha: dict[str, list[dict[str, Any]]] = {}
    for r in _runs(repo, pages):
        by_sha.setdefault(r["head_sha"], []).append(r)
    now = time.time()
    candidates: list[Case] = []
    for sha, runs in by_sha.items():
        if sha in skip:
            continue
        first = min(r["created_at"] for r in runs)
        # Leave very recent commits out: some of their workflows may still be running.
        if now - _ts(first) < min_age_hours * 3600:
            continue
        latest: dict[str, dict[str, Any]] = {}
        for r in runs:
            if r.get("conclusion") not in CONCLUSIVE:   # action_required (never ran), cancelled, ...
                continue
            wf = Path(r["path"]).name
            if wf not in latest or r["run_attempt"] > latest[wf]["run_attempt"] or r["id"] > latest[wf]["id"]:
                latest[wf] = r
        if len(latest) < 3:   # too few workflows: a docs-only or partial run, not a full CI picture
            continue
        candidates.append(Case(sha=sha, title=runs[0].get("display_title", ""), created_at=first,
                               failed=any(r["conclusion"] == "failure" for r in latest.values()),
                               workflows=sorted(latest)))
        candidates[-1]._runs = list(latest.values())  # type: ignore[attr-defined]
    candidates.sort(key=lambda c: c.created_at, reverse=True)
    want_fail = round(count * failing_share)
    failing = [c for c in candidates if c.failed][:want_fail]
    passing = [c for c in candidates if not c.failed][:count - len(failing)]
    picked = sorted(failing + passing, key=lambda c: c.created_at, reverse=True)
    for c in picked:
        for r in c._runs:  # type: ignore[attr-defined]
            for j in github.paginate(f"repos/{repo}/actions/runs/{r['id']}/jobs?filter=latest", key="jobs"):
                c.jobs.append(GhJob(workflow=Path(r["path"]).name, name=j["name"],
                                    conclusion=j.get("conclusion") or "", seconds=_seconds(j),
                                    url=j.get("html_url", "")))
    return picked


def _ts(s: str) -> float:
    return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))


def _seconds(j: dict[str, Any]) -> float:
    try:
        return max(0.0, _ts(j["completed_at"]) - _ts(j["started_at"]))
    except (KeyError, TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- the clone

def _git(root: Path, *args: str, check_: bool = True) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "preflight", "GIT_AUTHOR_EMAIL": "preflight@localhost",
           "GIT_COMMITTER_NAME": "preflight", "GIT_COMMITTER_EMAIL": "preflight@localhost",
           "GIT_TERMINAL_PROMPT": "0"}
    p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, env=env)
    if check_ and p.returncode != 0:
        raise gitinfo.GitError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout.strip()


def ensure_clone(repo: str) -> Path:
    root = bench_dir(repo) / "repo"
    if not (root / ".git").exists():
        root.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout",
                        f"https://github.com/{repo}.git", str(root)], check=True)
    return root


def prepare(root: Path, case: Case, base_branch: str) -> tuple[str, str]:
    """Check out what GitHub tested. Returns (base commit, how it was built)."""
    _git(root, "fetch", "--quiet", "origin", base_branch, case.sha)
    _git(root, "reset", "--quiet", "--hard")
    _git(root, "clean", "-fdq")
    base = _git(root, "rev-list", "-1", "--first-parent", f"--before={case.created_at}", f"origin/{base_branch}")
    _git(root, "checkout", "--quiet", "--detach", base)
    merged = subprocess.run(["git", "-C", str(root), "-c", "user.name=preflight", "-c", "user.email=preflight@localhost",
                             "merge", "--quiet", "--no-ff", "--no-edit", case.sha], capture_output=True, text=True)
    if merged.returncode == 0:
        return base, "merge"
    _git(root, "merge", "--abort", check_=False)
    _git(root, "checkout", "--quiet", "--detach", case.sha)
    return _git(root, "merge-base", case.sha, f"origin/{base_branch}"), "head (merge conflicted)"


# ---------------------------------------------------------------- matching

def github_name(job, combo: dict[str, Any]) -> str:
    """The name GitHub gives this job run."""
    name = job.name or job.id
    if "${{" in name:
        try:
            return expr.interpolate(name, expr.Context(values={"matrix": combo}))
        except expr.ExpressionError:
            return name
    if not combo:
        return name
    # GitHub lists the matrix axes; keys an `include` adds to an existing
    # combination are not shown. A combination made only by `include` shows all.
    axes = [k for k in (job.matrix or {}) if k not in ("include", "exclude")] if isinstance(job.matrix, dict) else []
    keys = [k for k in axes if k in combo] or list(combo)
    return f"{name} ({', '.join(expr.to_str(combo[k]) for k in keys)})"


def _vals(name: str) -> tuple[str, frozenset[str]]:
    m = re.match(r"^(.*?) \((.*)\)$", name)
    if not m:
        return name, frozenset()
    return m.group(1), frozenset(v.strip() for v in m.group(2).split(","))


def match(gh_jobs: list[GhJob], wf_file: str, job, combo: dict[str, Any]) -> GhJob | None:
    want = github_name(job, combo)
    same_wf = [g for g in gh_jobs if g.workflow == wf_file]
    for g in same_wf:
        if g.name == want:
            return g
    base, vals = _vals(want)   # the same values in another order
    for g in same_wf:
        if _vals(g.name) == (base, vals):
            return g
    return None


# ---------------------------------------------------------------- running

@dataclass
class Row:
    sha: str
    title: str
    workflow: str
    job: str             # GitHub's job name
    github: str          # success | failure | ... | missing
    local: str           # passed | failed | cancelled | left-to-github | not-triggered | not-sampled
    reason: str = ""
    seconds: float = 0.0
    gh_seconds: float = 0.0
    log: str = ""
    url: str = ""
    built: str = ""


def run_case(repo: str, root: Path, case: Case, base_branch: str, per_case: int, max_minutes: float,
             jobs: int, only: list[str] | None, seed: int, want: set[tuple[str, str]] | None = None) -> list[Row]:
    """Replay one case. ``want`` = (workflow file, GitHub job name): run just those, report just those."""
    rows = _run_case(repo, root, case, base_branch, per_case, max_minutes, jobs, only, seed, want)
    return [r for r in rows if want is None or (r.workflow, r.job) in want]


def _run_case(repo: str, root: Path, case: Case, base_branch: str, per_case: int, max_minutes: float,
              jobs: int, only: list[str] | None, seed: int, want: set[tuple[str, str]] | None) -> list[Row]:
    base, built = prepare(root, case, base_branch)
    changed = gitinfo.changed_files(root, base, include_worktree=False)
    prof = profile.load(repo.split("/")[0])
    plan = check.build_plan(root, repo, f"origin/{base_branch}", changed, prof, only, quick=False, docker=True)
    plan.base_sha = base
    rows: list[Row] = []
    claimed: set[int] = set()
    chosen: dict[tuple[str, str], list[dict[str, Any]]] = {}
    candidates: list[tuple[Any, dict[str, Any], GhJob]] = []

    def row(wf_file: str, g: GhJob | None, name: str, local: str, reason: str = "") -> Row:
        return Row(case.sha, case.title, wf_file, g.name if g else name, g.conclusion if g else "missing",
                   local, reason, gh_seconds=g.seconds if g else 0.0, url=g.url if g else "", built=built)

    for p in plan.jobs:
        wf_file = Path(p.wf.path).name
        for combo in p.combos:
            g = match(case.jobs, wf_file, p.job, combo)
            if g:
                claimed.add(id(g))
            if p.status != "will-run":
                rows.append(row(wf_file, g, github_name(p.job, combo), "left-to-github", p.reason))
            elif g is None and wf_file not in case.workflows:
                rows.append(row(wf_file, None, github_name(p.job, combo), "not-sampled",
                                "no finished GitHub run of this workflow to compare with"))
            elif g is None:
                rows.append(row(wf_file, None, github_name(p.job, combo), "unmatched",
                                "PreFlight would run it; GitHub has no job with this name"))
            elif g.conclusion not in CONCLUSIVE:
                rows.append(row(wf_file, g, "", "not-sampled", f"GitHub result was {g.conclusion or 'none'}"))
            elif max_minutes and g.seconds > max_minutes * 60:
                rows.append(row(wf_file, g, "", "not-sampled", f"took {g.seconds / 60:.0f} min on GitHub"))
            else:
                candidates.append((p, combo, g))
    triggered_files = {Path(p.wf.path).name for p in plan.jobs}
    for g in case.jobs:
        if id(g) in claimed or g.conclusion not in CONCLUSIVE:
            continue
        if g.workflow not in triggered_files:
            rows.append(row(g.workflow, g, "", "not-triggered", "PreFlight thought this change does not trigger it"))
        else:
            rows.append(row(g.workflow, g, "", "unmatched", "no PreFlight job maps to this GitHub job name"))

    # Every job GitHub failed, plus a seeded random pick of the ones it passed.
    rng = random.Random(f"{seed}:{case.sha}")
    fails = [c for c in candidates if c[2].conclusion == "failure"]
    passes = [c for c in candidates if c[2].conclusion == "success"]
    rng.shuffle(passes)
    if want is not None:
        picked = [c for c in candidates if (Path(c[0].wf.path).name, c[2].name) in want]
    else:
        picked = fails + passes[:max(0, per_case - len(fails))] if per_case else candidates
    for c in candidates:
        if c not in picked:
            rows.append(row(Path(c[0].wf.path).name, c[2], "", "not-sampled", "not in this case's sample"))
    for p, combo, g in picked:
        chosen.setdefault((p.wf.path, p.job.id), []).append(combo)
    if not picked:
        return rows

    sub = check.Plan(repo=plan.repo, base=plan.base, changed=plan.changed, docker=True, base_sha=base)
    for p in plan.to_run:
        combos = chosen.get((p.wf.path, p.job.id))
        if combos:
            sub.jobs.append(check.PlannedJob(p.wf, p.job, combos, p.failures, "will-run"))
    results = check.execute(sub, root, fail_fast=False, verbose=False, snapshot="head", jobs=jobs)
    by_label = {(r.workflow, r.label): r for r in results}
    for p, combo, g in picked:
        r = by_label.get((p.wf.name, job_label(p.job, combo)))
        if r is None:
            rows.append(row(Path(p.wf.path).name, g, "", "cancelled", "did not run (a job it needs failed)"))
            continue
        fs = r.failed_step
        status = r.status
        if status == "failed" and fs and "set PREFLIGHT_GITHUB_TOKEN" in fs.detail:
            status = "needs-token"   # refused by GitHub's API for want of a token: not a verdict on the code
        elif status == "failed" and r.oom:
            status = "needs-memory"  # killed for memory even alone; GitHub's runner has 16 GB
        rw = row(Path(p.wf.path).name, g, "", status, f"'{fs.name}' {fs.detail}" if fs else "")
        rw.seconds, rw.log = r.seconds, str(r.log_path or "")
        rows.append(rw)
    return rows


def done_shas(results: Path) -> set[str]:
    if not results.exists():
        return set()
    return {json.loads(line)["sha"] for line in results.read_text().splitlines() if line.strip()}


def drop_disagreements(results: Path) -> int:
    """Forget every case with a disagreement, so the next run replays it."""
    rows = [json.loads(line) for line in results.read_text().splitlines() if line.strip()]
    bad = {r["sha"] for r in rows if r["local"] in ("passed", "failed") and r["github"] in CONCLUSIVE
           and not _agrees(r)}
    results.write_text("".join(json.dumps(r) + "\n" for r in rows if r["sha"] not in bad))
    return len(bad)


def run(repo: str, count: int, failing_share: float, per_case: int, max_minutes: float, jobs: int,
        only: list[str] | None, seed: int, base_branch: str | None, rerun_disagreements: bool = False) -> Path:
    d = bench_dir(repo)
    d.mkdir(parents=True, exist_ok=True)
    results = d / "results.jsonl"
    if rerun_disagreements and results.exists():
        print(f"Replaying {drop_disagreements(results)} cases with a disagreement.", flush=True)
    finished = done_shas(results)
    cases_file = d / "cases.json"
    if cases_file.exists():
        cases = [Case(**{**c, "jobs": [GhJob(**j) for j in c["jobs"]]}) for c in json.loads(cases_file.read_text())]
    else:
        print(f"Sampling {count} recent PR commits of {repo} from GitHub ...", flush=True)
        cases = sample_cases(repo, count, failing_share, pages=max(5, count // 2))
        cases_file.write_text(json.dumps([asdict(c) for c in cases], indent=1))
    base_branch = base_branch or github.api(f"repos/{repo}")["default_branch"]
    root = ensure_clone(repo)
    todo = [c for c in cases if c.sha not in finished]
    print(f"{len(cases)} cases ({sum(c.failed for c in cases)} with a failed GitHub run); "
          f"{len(cases) - len(todo)} already done.", flush=True)
    for i, case in enumerate(todo, 1):
        print(f"\n[{i}/{len(todo)}] {case.sha[:10]} {case.title[:70]}", flush=True)
        t = time.time()
        try:
            rows = run_case(repo, root, case, base_branch, per_case, max_minutes, jobs, only, seed)
        except (gitinfo.GitError, subprocess.CalledProcessError) as e:
            rows = [Row(case.sha, case.title, "", "", "", "error", str(e)[:300])]
        with results.open("a") as f:
            for r in rows:
                f.write(json.dumps(asdict(r)) + "\n")
        ran = [r for r in rows if r.local in ("passed", "failed")]
        agree = sum(_agrees(r) for r in ran)
        print(f"  case done in {(time.time() - t) / 60:.0f} min: {agree}/{len(ran)} agree", flush=True)
    return results


# ---------------------------------------------------------------- report

def _agrees(r: dict | Row) -> bool:
    d = asdict(r) if isinstance(r, Row) else r
    return (d["local"], d["github"]) in (("passed", "success"), ("failed", "failure"))


def _pct(a: int, b: int) -> str:
    return f"{a / b:.1%} ({a}/{b})" if b else "n/a (0 runs)"


def _oom_log(path: str) -> bool:
    try:
        return "Docker ran out of memory" in Path(path).read_text(errors="replace")[-2000:]
    except OSError:
        return False


# A GitHub failure whose log shows this was the runner failing to download
# something, not the code failing.
_GH_INFRA = re.compile(
    r"unresolved dependency|Could not resolve (dependencies|artifact)|Could not transfer artifact|"
    r"Could not find artifact|or one of its dependencies could not be resolved|Error grabbing Grapes|"
    r"returned error: 5\d\d|HTTP (Error )?5\d\d|Connection (reset|refused|timed out)|Read timed out|"
    r"Temporary failure in name resolution|Failed to connect to|No space left on device|"
    r"The runner has received a shutdown signal|lost communication with the server",
    re.I)


_SETUP_STEP = re.compile(r"\s*(set ?up|install|download|checkout|cache|restore|clone)\b", re.I)


def _github_infra(repo: str, url: str, cache: dict[str, str]) -> str:
    """The infrastructure error in GitHub's log for this job, or ""."""
    if url in cache:
        return cache[url]
    job_id = url.rstrip("/").rsplit("/", 1)[-1]
    try:
        steps = (github.api(f"repos/{repo}/actions/jobs/{job_id}") or {}).get("steps") or []
    except github.GitHubError:
        steps = []
    first = next((st["name"] for st in steps if st.get("conclusion") == "failure"), "")
    if _SETUP_STEP.match(first):
        cache[url] = f"GitHub's machine-setup step '{first}' failed, before the check ran"
        return cache[url]
    try:
        log = github.api(f"repos/{repo}/actions/jobs/{job_id}/logs", raw=True) or ""
    except github.GitHubError:
        log = ""
    hit = ""
    for line in log.splitlines():
        if _GH_INFRA.search(line):
            hit = line.split("Z ", 1)[-1].strip()[:160]
            break
    cache[url] = hit
    return hit


def report(repo: str, target: float = 0.95) -> str:
    rows = [json.loads(line) for line in (bench_dir(repo) / "results.jsonl").read_text().splitlines() if line.strip()]
    for r in rows:   # rows written before needs-memory existed
        if r["local"] == "failed" and r.get("log") and _oom_log(r["log"]):
            r["local"] = "needs-memory"
    cache_file = bench_dir(repo) / "github-infra.json"
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    infra: list[dict] = []
    for r in rows:   # passed here, failed on GitHub: did GitHub's runner fail rather than the code?
        if r["local"] == "passed" and r["github"] == "failure" and r.get("url"):
            why = _github_infra(repo, r["url"], cache)
            if why:
                r["github"], r["reason"] = "infra-failure", why
                infra.append(r)
    cache_file.write_text(json.dumps(cache, indent=1))
    ran = [r for r in rows if r["local"] in ("passed", "failed") and r["github"] in CONCLUSIVE]
    gh_pass = [r for r in ran if r["github"] == "success"]
    gh_fail = [r for r in ran if r["github"] == "failure"]
    agree = sum(_agrees(r) for r in ran)
    conclusive = [r for r in rows if r["github"] in CONCLUSIVE]
    covered = [r for r in conclusive if r["local"] not in ("left-to-github", "not-triggered", "unmatched", "needs-token", "needs-memory")]
    trig = [r for r in conclusive if r["local"] != "unmatched"]
    trig_ok = [r for r in trig if r["local"] != "not-triggered"]
    cases = {r["sha"] for r in rows}
    acc = agree / len(ran) if ran else 0.0
    lines = [
        f"# PreFlight accuracy on {repo}",
        "",
        f"{len(cases)} PR commits, {len(ran)} job runs run locally and compared with GitHub.",
        "",
        f"**Agreement: {_pct(agree, len(ran))}** (target {target:.0%}: "
        f"{'met' if ran and acc >= target else 'not met'})",
        "",
        "| Measure | Value |",
        "| --- | --- |",
        f"| Agreement on jobs GitHub passed (no false alarms) | {_pct(sum(_agrees(r) for r in gh_pass), len(gh_pass))} |",
        f"| Agreement on jobs GitHub failed (failures caught) | {_pct(sum(_agrees(r) for r in gh_fail), len(gh_fail))} |",
        f"| Missed failures (local pass, GitHub fail) | {sum(r['local'] == 'passed' for r in gh_fail)} |",
        f"| False alarms (local fail, GitHub pass) | {sum(r['local'] == 'failed' for r in gh_pass)} |",
        f"| Coverage: GitHub jobs PreFlight can run | {_pct(len(covered), len(conclusive))} |",
        f"| Trigger accuracy: GitHub jobs PreFlight knew would run | {_pct(len(trig_ok), len(trig))} |",
        f"| GitHub failures caused by GitHub's runner (download/network), not compared | {len(infra)} |",
        f"| PreFlight jobs with no GitHub job of the same name | {sum(r['local'] == 'unmatched' and r['github'] == 'missing' for r in rows)} |",
        "",
        "Failing PRs are sampled on purpose (every job GitHub failed is run), so the failed-job row has",
        "enough runs to mean something; the overall figure is not weighted to the real pass/fail mix.",
    ]
    bad = [r for r in ran if not _agrees(r)]
    if bad:
        lines += ["", "## Disagreements", "", "| Commit | Job | GitHub | Local | Local failure |", "| --- | --- | --- | --- | --- |"]
        for r in bad:
            lines.append(f"| {r['sha'][:10]} | {r['workflow']} / {r['job']} | [{r['github']}]({r['url']}) | "
                         f"{r['local']} | {r['reason'].replace('|', '/')[:140]} |")
    if infra:
        lines += ["", "## GitHub infrastructure failures (passed here; GitHub's own log shows a download or network error)",
                  "", "| Commit | Job | GitHub's error |", "| --- | --- | --- |"]
        lines += [f"| {r['sha'][:10]} | [{r['workflow']} / {r['job']}]({r['url']}) | {r['reason'].replace('|', '/')} |"
                  for r in infra]
    left: dict[str, int] = {}
    for r in conclusive:
        if r["local"] in ("left-to-github", "not-triggered", "unmatched", "needs-token", "needs-memory"):
            key = "needs-token: set PREFLIGHT_GITHUB_TOKEN" if r["local"] == "needs-token" else \
                "needs-memory: Docker ran out of memory even with the job alone" if r["local"] == "needs-memory" else \
                f"{r['local']}: {r['reason'].split(':')[0] if r['local'] == 'left-to-github' else r['workflow']}"
            left[key] = left.get(key, 0) + 1
    if left:
        lines += ["", "## Not run locally", "", "| Why | GitHub job runs |", "| --- | --- |"]
        lines += [f"| {k} | {v} |" for k, v in sorted(left.items(), key=lambda kv: -kv[1])]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- fast follow-ups

def _load_cases(d: Path) -> list[Case]:
    f = d / "cases.json"
    if not f.exists():
        return []
    return [Case(**{**c, "jobs": [GhJob(**j) for j in c["jobs"]]}) for c in json.loads(f.read_text())]


def _rows(results: Path) -> list[dict]:
    return [json.loads(line) for line in results.read_text().splitlines() if line.strip()] if results.exists() else []


def replay_disagreements(repo: str, jobs: int, base_branch: str) -> None:
    """Run again only the job runs that disagreed, replacing their rows."""
    d = bench_dir(repo)
    results = d / "results.jsonl"
    rows = _rows(results)
    for r in rows:   # killed for memory even alone: replaying it would only run out again
        if r["local"] == "failed" and r.get("log") and _oom_log(r["log"]):
            r["local"] = "needs-memory"
    bad = [r for r in rows if r["local"] in ("passed", "failed") and r["github"] in CONCLUSIVE and not _agrees(r)]
    cases = {c.sha: c for c in _load_cases(d)}
    root = ensure_clone(repo)
    for sha in dict.fromkeys(r["sha"] for r in bad):
        want = {(r["workflow"], r["job"]) for r in bad if r["sha"] == sha}
        print(f"\nReplaying {len(want)} job run(s) of {sha[:10]}", flush=True)
        new = [asdict(r) for r in run_case(repo, root, cases[sha], base_branch, 0, 0, jobs, None, 1, want)]
        redone = {(r["sha"], r["workflow"], r["job"]) for r in new}
        rows = [r for r in rows if (r["sha"], r["workflow"], r["job"]) not in redone] + new
        results.write_text("".join(json.dumps(r) + "\n" for r in rows))   # saved per case: safe to stop


def add_failures(repo: str, count: int, max_minutes: float, jobs: int, base_branch: str, pages: int = 40) -> None:
    """Add PR commits with a failed job that PreFlight can run, and run only those failed jobs.

    Workflows the results so far show are left to GitHub are not searched.
    """
    d = bench_dir(repo)
    results = d / "results.jsonl"
    rows = _rows(results)
    left = {r["workflow"] for r in rows if r["local"] in ("left-to-github", "needs-token", "needs-memory")}
    runnable = {r["workflow"] for r in rows if r["local"] in ("passed", "failed")}
    known = {r["sha"] for r in rows}
    cases = _load_cases(d)
    root = ensure_clone(repo)
    cache_file = d / "github-infra.json"
    infra_cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    found = 0
    for page in range(1, pages + 1):
        data = github.api(f"repos/{repo}/actions/runs?event=pull_request&status=failure&per_page=100&page={page}")
        for run in data.get("workflow_runs", []):
            wf = Path(run["path"]).name
            if found >= count or run["head_sha"] in known or wf in left or wf not in runnable:
                continue
            gh = [GhJob(wf, j["name"], j.get("conclusion") or "", _seconds(j), j.get("html_url", ""))
                  for j in github.paginate(f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest", key="jobs")]
            failed = [g for g in gh if g.conclusion == "failure" and (not max_minutes or g.seconds <= max_minutes * 60)
                      and not _github_infra(repo, g.url, infra_cache)]   # a code failure, not a GitHub flake
            if not failed:
                continue
            case = Case(sha=run["head_sha"], title=run.get("display_title", ""), created_at=run["created_at"],
                        failed=True, workflows=[wf], jobs=gh)
            known.add(case.sha)
            cache_file.write_text(json.dumps(infra_cache, indent=1))
            print(f"\n[failure {found + 1}/{count}] {case.sha[:10]} {wf}: "
                  f"{', '.join(g.name for g in failed)} — {case.title[:60]}", flush=True)
            try:
                got = run_case(repo, root, case, base_branch, 0, 0, jobs, None, 1, {(wf, g.name) for g in failed})
            except (gitinfo.GitError, subprocess.CalledProcessError) as e:
                print(f"  skipped: {str(e)[:200]}", flush=True)
                continue
            if not any(r.local in ("passed", "failed") for r in got):
                print("  nothing comparable (job left to GitHub or not matched)", flush=True)
                continue
            found += 1
            cases.append(case)
            (d / "cases.json").write_text(json.dumps([asdict(c) for c in cases], indent=1))
            with results.open("a") as f:
                for r in got:
                    f.write(json.dumps(asdict(r)) + "\n")
            ok = sum(_agrees(r) for r in got)
            print(f"  {ok}/{len(got)} agree", flush=True)
        if found >= count:
            break


def replan(repo: str, base_branch: str) -> int:
    """Re-plan every saved case with the current PreFlight (no jobs run).

    Rows for jobs it now leaves to GitHub become left-to-github, so a verdict
    change is measured on old results too. Returns how many rows changed.
    """
    d = bench_dir(repo)
    results = d / "results.jsonl"
    rows = _rows(results)
    root = ensure_clone(repo)
    prof = profile.load(repo.split("/")[0])
    changed = 0
    for case in _load_cases(d):
        mine = [r for r in rows if r["sha"] == case.sha and r["local"] in ("passed", "failed", "needs-token", "needs-memory")]
        if not mine:
            continue
        base, _ = prepare(root, case, base_branch)
        plan = check.build_plan(root, repo, f"origin/{base_branch}", gitinfo.changed_files(root, base, False),
                                prof, None, quick=False, docker=True)
        left = {}
        for p in plan.jobs:
            if p.status != "will-run":
                for combo in p.combos:
                    left[(Path(p.wf.path).name, github_name(p.job, combo))] = p.reason
        for r in mine:
            why = left.get((r["workflow"], r["job"]))
            if why:
                r["local"], r["reason"] = "left-to-github", why
                changed += 1
    results.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return changed
