"""``preflight check``: run the CI a PR from this branch would trigger."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import expr, gitinfo, globs, profile, workflow
from .learn import host_os
from .runner import JobResult, job_label, run_job


@dataclass
class PlannedJob:
    wf: workflow.Workflow
    job: workflow.Job
    combos: list[dict[str, Any]]
    failures: int
    status: str            # will-run | skip
    reason: str = ""


@dataclass
class Plan:
    repo: str
    base: str
    changed: list[str]
    jobs: list[PlannedJob] = field(default_factory=list)
    not_triggered: list[tuple[str, str]] = field(default_factory=list)  # (workflow, why)
    other_ci: list[str] = field(default_factory=list)

    @property
    def to_run(self) -> list[PlannedJob]:
        return [j for j in self.jobs if j.status == "will-run"]

    @property
    def skipped(self) -> list[PlannedJob]:
        return [j for j in self.jobs if j.status == "skip"]


def pick_upstream(root: Path, repo_arg: str | None) -> tuple[str, str | None]:
    """Return (``owner/repo`` the PR targets, remote name for it)."""
    rem = gitinfo.remotes(root)
    if repo_arg:
        remote = next((n for n, s in rem.items() if s.lower() == repo_arg.lower()), None)
        return repo_arg, remote
    for name in ("upstream", "origin"):
        if name in rem:
            return rem[name], name
    if rem:
        name, slug = next(iter(rem.items()))
        return slug, name
    raise SystemExit("preflight: no GitHub remote found; pass --repo owner/name")


def resolve_base(root: Path, remote: str | None, prof: dict | None, repo: str, base_arg: str | None) -> str:
    if base_arg:
        return base_arg
    branch = None
    if prof:
        branch = prof.get("repos", {}).get(repo.split("/")[1].lower(), {}).get("default_branch")
    if remote and not branch:
        branch = gitinfo.remote_default_branch(root, remote)
    for candidate in ([f"{remote}/{branch}"] if remote and branch else []) + [
        f"{remote}/main" if remote else "", f"{remote}/master" if remote else "", "main", "master"
    ]:
        if candidate and gitinfo.ref_exists(root, candidate):
            return candidate
    raise SystemExit("preflight: cannot find the base branch; pass --base <ref> (for example upstream/main)")


def github_context(root: Path, repo: str, base: str, event_name: str) -> dict[str, Any]:
    head = gitinfo.current_branch(root)
    base_branch = base.split("/", 1)[-1]
    owner = repo.split("/")[0]
    return {
        "github": {
            "event_name": event_name,
            "repository": repo,
            "repository_owner": owner,
            "ref": "refs/pull/0/merge",
            "sha": gitinfo.head_sha(root),
            "base_ref": base_branch,
            "head_ref": head,
            "actor": "preflight",
            "workspace": str(root),
            "workflow": "",
            "event": {
                "action": "synchronize",
                "number": 0,
                "pull_request": {
                    "number": 0,
                    "draft": False,
                    "head": {"ref": head, "repo": {"fork": True}},
                    "base": {"ref": base_branch, "repo": {"full_name": repo}},
                    "labels": [],
                },
            },
        }
    }


def build_plan(root: Path, repo: str, base: str, changed: list[str], prof: dict | None,
               only: list[str] | None, quick: bool) -> Plan:
    plan = Plan(repo=repo, base=base, changed=changed)
    if prof:
        entry = prof.get("repos", {}).get(repo.split("/")[1].lower(), {})
        plan.other_ci = entry.get("other_ci", [])
    host = host_os()
    for wf in workflow.load_dir(root / ".github" / "workflows"):
        rel = Path(wf.path).name
        event = wf.pr_event
        if not event:
            continue
        types = wf.pr_types()
        if types and not ({"synchronize", "opened"} & set(types)):
            plan.not_triggered.append((wf.name, f"runs only on PR {', '.join(types)}"))
            continue
        paths, ignore = wf.pr_filters()
        if not globs.triggers(changed, paths, ignore):
            plan.not_triggered.append((wf.name, "your change does not touch its paths"))
            continue
        ctx_values = github_context(root, repo, base, event)
        for job in wf.jobs:
            if only and not any(o.lower() in (job.id.lower(), wf.name.lower(), rel.lower()) for o in only):
                continue
            workflow.classify(job, host)
            fails = profile.failure_count(prof, repo, wf.path, job.id)
            combos = workflow.expand_matrix(job.matrix) if not isinstance(job.matrix, str) else [{}]
            if quick:
                combos = combos[:1]
            pj = PlannedJob(wf=wf, job=job, combos=combos, failures=fails, status="will-run")
            if job.verdict not in workflow.RUNNABLE:
                pj.status, pj.reason = "skip", f"{job.verdict}: {'; '.join(job.reasons)}"
            else:
                try:
                    ok = expr.evaluate_condition(job.if_, expr.Context(values=ctx_values))
                except expr.ExpressionError as e:
                    ok, pj.reason = True, f"could not read job `if:` ({e}); running it"
                if not ok:
                    pj.status, pj.reason = "skip", "job condition is false for a PR"
            plan.jobs.append(pj)
    # Likely failures first, then smaller jobs, so a red result comes early.
    plan.jobs.sort(key=lambda p: (-p.failures, len(p.combos) * len(p.job.steps)))
    return plan


def print_plan(plan: Plan) -> None:
    print(f"Repo {plan.repo}, comparing with {plan.base}: {len(plan.changed)} changed files")
    if plan.to_run:
        os_notes = {r for p in plan.to_run for r in p.job.reasons if r.startswith("Linux job run on")}
        for r in sorted(os_notes):
            print(f"Note: {r.replace('Linux job run on', 'Linux jobs run on')}")
        print(f"\nWill run ({sum(len(p.combos) for p in plan.to_run)} job runs):")
        for p in plan.to_run:
            hist = f"  [failed {p.failures}x on recent PRs]" if p.failures else ""
            extra = f"  x{len(p.combos)} matrix" if len(p.combos) > 1 else ""
            print(f"  - {p.wf.name} / {p.job.id}{extra}{hist}")
            for r in p.job.reasons:
                if r not in os_notes:
                    print(f"      note: {r}")
    if plan.skipped:
        print("\nCannot run here (GitHub will run these):")
        for p in plan.skipped:
            print(f"  - {p.wf.name} / {p.job.id}: {p.reason}")
    if plan.not_triggered:
        print("\nNot triggered by this change:")
        for name, why in plan.not_triggered:
            print(f"  - {name}: {why}")
    if plan.other_ci:
        print(f"\nAlso uses {', '.join(plan.other_ci)} (not run locally yet).")


def _needs_order(jobs: list[PlannedJob]) -> list[PlannedJob]:
    """Keep the ranked order but put each job after the jobs it `needs`."""
    by_key = {(p.wf.path, p.job.id): p for p in jobs}
    done: set[tuple[str, str]] = set()
    out: list[PlannedJob] = []

    def visit(p: PlannedJob, stack: set) -> None:
        key = (p.wf.path, p.job.id)
        if key in done or key in stack:
            return
        stack.add(key)
        for n in p.job.needs:
            dep = by_key.get((p.wf.path, n))
            if dep:
                visit(dep, stack)
        done.add(key)
        out.append(p)

    for p in jobs:
        visit(p, set())
    return out


def execute(plan: Plan, root: Path, fail_fast: bool, verbose: bool) -> list[JobResult]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir = profile.home() / "logs" / plan.repo.replace("/", "_") / stamp
    results: list[JobResult] = []
    outcome: dict[tuple[str, str], str] = {}
    host = host_os()
    for p in _needs_order(plan.to_run):
        blocked = [n for n in p.job.needs if outcome.get((p.wf.path, n)) not in (None, "passed")]
        if blocked:
            outcome[(p.wf.path, p.job.id)] = "blocked"
            print(f"  - {p.wf.name} / {p.job.id}: not run, needs {', '.join(blocked)}")
            continue
        ctx_values = github_context(root, plan.repo, plan.base, p.wf.pr_event or "pull_request")
        job_ok = True
        for combo in p.combos:
            label = job_label(p.job, combo)
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in f"{Path(p.wf.path).stem}-{label}")
            print(f"  ▶ {p.wf.name} / {label} ...", flush=True)
            res = run_job(p.wf, p.job, combo, ctx_values, root, log_dir / f"{safe}.log", host, echo=verbose)
            results.append(res)
            mark = "✓" if res.status == "passed" else "✗"
            tail = ""
            if res.failed_step:
                tail = f": '{res.failed_step.name}' {res.failed_step.detail}"
            print(f"    {mark} {res.status} in {res.seconds:.0f}s{tail}")
            if res.status != "passed":
                job_ok = False
                _print_log_tail(res.log_path)
                if fail_fast:
                    break
        outcome[(p.wf.path, p.job.id)] = "passed" if job_ok else "failed"
        if not job_ok and fail_fast:
            print("  Stopping at the first failure (--keep-going runs everything).")
            break
    return results


def _print_log_tail(path: Path | None, lines: int = 25) -> None:
    if not path or not path.exists():
        return
    tail = path.read_text(errors="replace").splitlines()[-lines:]
    print("      ┌─ last lines of the log")
    for line in tail:
        print("      │ " + line)
    print(f"      └─ full log: {path}")


def write_report(plan: Plan, results: list[JobResult], path: Path, root: Path) -> None:
    passed = [r for r in results if r.status == "passed"]
    failed = [r for r in results if r.status != "passed"]
    lines = [
        "## PreFlight local CI report",
        "",
        f"Checked `{gitinfo.head_sha(root)[:12]}` against `{plan.base}` on {host_os()}, "
        f"{time.strftime('%Y-%m-%d %H:%M')}.",
        "",
        f"**{len(passed)} of {len(results)} job runs passed locally.**",
        "",
        "| Check | Result |",
        "| --- | --- |",
    ]
    for r in results:
        lines.append(f"| {r.workflow} / {r.label} | {'passed' if r.status == 'passed' else 'failed'} |")
    if plan.skipped:
        lines += ["", "Not run locally (left to GitHub):", ""]
        lines += [f"- {p.wf.name} / {p.job.id}: {p.reason}" for p in plan.skipped]
    if plan.other_ci:
        lines += ["", f"Other CI not run locally: {', '.join(plan.other_ci)}."]
    if failed:
        lines += ["", "Failed:", ""] + [f"- {r.workflow} / {r.label}" for r in failed]
    path.write_text("\n".join(lines) + "\n")
