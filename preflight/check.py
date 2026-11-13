"""``preflight check``: run the CI a PR from this branch would trigger."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import docker as dockermod
from . import expr, gitinfo, globs, profile, workflow
from .learn import host_os
from .runner import JobResult, StepResult, job_label, run_job


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
    docker: bool = False
    base_sha: str | None = None   # merge base of the branch and its base, the PR's parent commit

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
               only: list[str] | None, quick: bool, docker: bool = False) -> Plan:
    plan = Plan(repo=repo, base=base, changed=changed, docker=docker)
    try:
        plan.base_sha = gitinfo.git(root, "merge-base", base, "HEAD").strip()
    except gitinfo.GitError:
        plan.base_sha = None
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
            fails = profile.failure_count(prof, repo, wf.path, job.id)
            combos = workflow.expand_matrix(job.matrix) if not isinstance(job.matrix, str) else [{}]
            if quick:
                combos = combos[:1]
            for job, combos in _runner_groups(job, combos, ctx_values):
                workflow.classify(job, host, docker, root)
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


def _runner_groups(job: workflow.Job, combos: list[dict[str, Any]],
                   ctx_values: dict[str, Any]) -> list[tuple[workflow.Job, list[dict[str, Any]]]]:
    """Split a job whose ``runs-on`` reads the matrix into one job per runner.

    ``runs-on: ${{ matrix.os || 'ubuntu-latest' }}`` gives each matrix entry its own
    runner, so the Linux entries can run here while the Windows and macOS ones stay on GitHub.
    """
    if "matrix." not in json.dumps(job.runs_on, default=str) or combos == [{}]:
        return [(job, combos)]

    def resolve(value: Any, combo: dict[str, Any]) -> Any:
        if isinstance(value, str) and "${{" in value:
            ctx = expr.Context(values={**ctx_values, "matrix": combo})
            text = value.strip()
            if text.startswith("${{") and text.endswith("}}") and text.count("${{") == 1:
                return expr.evaluate(text[3:-2], ctx)
            return expr.interpolate(value, ctx)
        if isinstance(value, list):
            return [resolve(v, combo) for v in value]
        return value

    groups: dict[str, tuple[Any, list[dict[str, Any]]]] = {}
    for combo in combos:
        try:
            runs_on = resolve(job.runs_on, combo)
        except expr.ExpressionError:
            return [(job, combos)]
        key = json.dumps(runs_on, sort_keys=True, default=str)
        groups.setdefault(key, (runs_on, []))[1].append(combo)
    return [(dataclasses.replace(job, runs_on=runs_on, reasons=[]), cs) for runs_on, cs in groups.values()]


def print_plan(plan: Plan) -> None:
    print(f"Repo {plan.repo}, comparing with {plan.base}: {len(plan.changed)} changed files")
    print("Mode: " + ("Docker (each job in a fresh Ubuntu container)" if plan.docker
                      else f"native (steps run directly on this {host_os()} machine; --docker isolates them)"))
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
    if plan.docker and os.environ.get("PREFLIGHT_GITHUB_TOKEN"):
        print("\nJobs get PREFLIGHT_GITHUB_TOKEN as github.token (use a token with no scopes).")
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


class _Active:
    """Containers of running jobs, so a fail-fast cancel can stop them at once."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.boxes: set = set()

    def add(self, box) -> None:
        with self.lock:
            self.boxes.add(box)

    def remove(self, box) -> None:
        with self.lock:
            self.boxes.discard(box)

    def stop_all(self) -> None:
        with self.lock:
            boxes = list(self.boxes)
        for box in boxes:
            box.stop()


def _run_one(p: PlannedJob, combo: dict[str, Any], ctx_values: dict[str, Any], root: Path, log: Path,
             plan: Plan, verbose: bool, snapshot: str, cancel: threading.Event, active: _Active,
             say, echo_prefix: str) -> JobResult:
    label = job_label(p.job, combo)
    if not plan.docker:
        return run_job(p.wf, p.job, combo, ctx_values, root, log, host_os(), echo=verbose,
                       cancel=cancel, echo_prefix=echo_prefix)
    ctx = expr.Context(values=dict(ctx_values, matrix=combo, env={}, secrets={}, vars={}, inputs={}))
    box = None
    try:
        box = dockermod.DockerJob(p.job, combo, ctx, plan.repo, root, snapshot, say=say,
                                  base_sha=plan.base_sha)
        active.add(box)
        if cancel.is_set():
            return JobResult(p.wf.name, p.job.id, label, "cancelled", log_path=None)
        box.start()
        res = run_job(p.wf, p.job, combo, ctx_values, root, log, "Linux", echo=verbose, box=box,
                      cancel=cancel, echo_prefix=echo_prefix)
        if res.status == "failed" and box.oom_kills():
            res.oom = True
            with log.open("a") as f:
                f.write("! the kernel killed a process in this job: Docker ran out of memory\n")
        return res
    except (dockermod.DockerError, subprocess.SubprocessError, OSError) as e:
        if cancel.is_set():
            return JobResult(p.wf.name, p.job.id, label, "cancelled", log_path=None)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"# {p.wf.name} / {label}\n! docker: {e}\n")
        return JobResult(p.wf.name, p.job.id, label, "failed",
                         [StepResult("start container", "failed", str(e).splitlines()[0][:200])], log)
    finally:
        if box is not None:
            active.remove(box)
            box.stop()


def execute(plan: Plan, root: Path, fail_fast: bool, verbose: bool, snapshot: str = "worktree",
            jobs: int = 1) -> list[JobResult]:
    """Run the plan, up to ``jobs`` job runs at a time (Docker mode only).

    A job starts once every job it ``needs`` has passed. With ``fail_fast``,
    the first failure cancels the rest: running containers are stopped and
    nothing new starts.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir = profile.home() / "logs" / plan.repo.replace("/", "_") / stamp
    dockermod.clear_snapshot_cache()
    jobs = max(1, jobs if plan.docker else 1)  # native jobs share one checkout
    order = _needs_order(plan.to_run)
    keys = {(p.wf.path, p.job.id) for p in order}
    pending = [(p, combo) for p in order for combo in p.combos]
    left = {(p.wf.path, p.job.id): len(p.combos) for p in order}   # unfinished runs per job
    outcome: dict[tuple[str, str], str] = {}                      # job -> passed | failed | blocked
    results: list[tuple[int, JobResult]] = []
    order_index = {id(u): i for i, u in enumerate(pending)}
    order_index_units = list(pending)
    lock = threading.Lock()
    cancel = threading.Event()
    active = _Active()
    parallel = jobs > 1

    def say(msg: str) -> None:
        with lock:
            print(msg, flush=True)

    def deps(p: PlannedJob) -> list[tuple[str, str]]:
        return [(p.wf.path, n) for n in p.job.needs if (p.wf.path, n) in keys]

    def job_done(key: tuple[str, str]) -> None:
        if left[key] == 0 and key not in outcome:
            outcome[key] = "passed"

    if parallel:
        print(f"  ({jobs} job runs at a time; --jobs changes this)")
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        running: dict[Future, tuple[PlannedJob, dict[str, Any]]] = {}
        while pending or running:
            # Start every run whose needs have passed, in ranked order.
            for unit in list(pending):
                if len(running) >= jobs or cancel.is_set():
                    break
                p, combo = unit
                key = (p.wf.path, p.job.id)
                states = [outcome.get(d) for d in deps(p)]
                if any(s in ("failed", "blocked") for s in states):
                    pending.remove(unit)
                    left[key] -= 1
                    if outcome.get(key) != "blocked":
                        outcome[key] = "blocked"
                        bad = [d[1] for d, s in zip(deps(p), states) if s in ("failed", "blocked")]
                        say(f"  - {p.wf.name} / {p.job.id}: not run, needs {', '.join(bad)}")
                    continue
                if any(s != "passed" for s in states):
                    continue  # a dependency is still running
                pending.remove(unit)
                label = job_label(p.job, combo)
                safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in f"{Path(p.wf.path).stem}-{label}")
                say(f"  ▶ {p.wf.name} / {label}" + (" started" if parallel else " ..."))
                ctx_values = github_context(root, plan.repo, plan.base, p.wf.pr_event or "pull_request")
                prefix = f"    [{label}] " if parallel else "    "
                fut = pool.submit(_run_one, p, combo, ctx_values, root, log_dir / f"{safe}.log", plan,
                                  verbose, snapshot, cancel, active, say, prefix)
                running[fut] = unit
            if cancel.is_set() and pending:
                for p, _ in pending:
                    left[(p.wf.path, p.job.id)] -= 1
                pending.clear()
            if not running:
                if pending:  # nothing can start: a needs cycle or a missing job
                    for p, _ in pending:
                        say(f"  - {p.wf.name} / {p.job.id}: not run, its needs never finished")
                    pending.clear()
                break
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for fut in done:
                unit = running.pop(fut)
                p, combo = unit
                key = (p.wf.path, p.job.id)
                res = fut.result()
                results.append((order_index[id(unit)], res))
                left[key] -= 1
                with lock:
                    if res.status == "cancelled":
                        print(f"  ■ {p.wf.name} / {res.label}: cancelled")
                    else:
                        mark = "✓" if res.status == "passed" else "✗"
                        tail = f": '{res.failed_step.name}' {res.failed_step.detail}" if res.failed_step else ""
                        print(f"  {mark} {p.wf.name} / {res.label} {res.status} in {res.seconds:.0f}s{tail}")
                    if res.status == "failed":
                        _print_log_tail(res.log_path)
                if res.status == "failed":
                    outcome[key] = "failed"
                    # Out of memory next to other jobs: it is retried alone below, so no cancel yet.
                    if fail_fast and not cancel.is_set() and not (res.oom and parallel):
                        cancel.set()
                        say("  Stopping at the first failure (--keep-going runs everything).")
                        active.stop_all()
                job_done(key)
    # A job that ran out of memory next to others gets one more try on its own:
    # GitHub gives every job a whole machine.
    # With --keep-going every failure is confirmed alone: timing checks and tight
    # timeouts can fail just from sharing the machine.
    retry = [(i, r) for i, r in results if parallel and r.status == "failed" and (r.oom or not fail_fast)
             and not (fail_fast and cancel.is_set())]
    if retry:
        why = "ran out of Docker memory" if all(r.oom for _, r in retry) else "failed"
        say(f"  {len(retry)} job run(s) {why} while others ran; running them again one at a time to confirm.")
        units = dict(enumerate(order_index_units))
        for i, old in retry:
            p, combo = units[i]
            label = job_label(p.job, combo)
            say(f"  ▶ {p.wf.name} / {label} (alone)")
            ctx_values = github_context(root, plan.repo, plan.base, p.wf.pr_event or "pull_request")
            res = _run_one(p, combo, ctx_values, root, old.log_path or log_dir / f"retry-{i}.log", plan,
                           verbose, snapshot, cancel, active, say, "    ")
            mark = "✓" if res.status == "passed" else "✗"
            tail = f": '{res.failed_step.name}' {res.failed_step.detail}" if res.failed_step else ""
            say(f"  {mark} {p.wf.name} / {res.label} {res.status} in {res.seconds:.0f}s{tail}"
                + (" (out of memory even alone)" if res.oom else ""))
            results = [(j, res if j == i else r) for j, r in results]
    # Report in plan order, whatever order they finished in.
    return [r for _, r in sorted(results, key=lambda x: x[0])]


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
    failed = [r for r in results if r.status == "failed" and not r.oom]
    no_memory = [r for r in results if r.status == "failed" and r.oom]
    cancelled = [r for r in results if r.status == "cancelled"]
    lines = [
        "## PreFlight local CI report",
        "",
        f"Checked `{gitinfo.head_sha(root)[:12]}` against `{plan.base}` "
        f"{'in Docker (Ubuntu containers)' if plan.docker else 'on ' + host_os()}, "
        f"{time.strftime('%Y-%m-%d %H:%M')}.",
        "",
        f"**{len(passed)} of {len(results) - len(no_memory)} checked job runs passed locally.**",
        "",
        "| Check | Result |",
        "| --- | --- |",
    ]
    for r in results:
        lines.append(f"| {r.workflow} / {r.label} | {'not checked (out of memory)' if r.oom else r.status} |")
    if plan.skipped:
        lines += ["", "Not run locally (left to GitHub):", ""]
        lines += [f"- {p.wf.name} / {p.job.id}: {p.reason}" for p in plan.skipped]
    if plan.other_ci:
        lines += ["", f"Other CI not run locally: {', '.join(plan.other_ci)}."]
    if failed:
        lines += ["", "Failed:", ""] + [f"- {r.workflow} / {r.label}" for r in failed]
    if cancelled:
        lines += ["", f"{len(cancelled)} job runs were cancelled after the first failure."]
    path.write_text("\n".join(lines) + "\n")
