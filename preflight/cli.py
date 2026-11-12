"""Command line entry point: ``preflight learn | show | plan | check | install-hook``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, bench, check, docker, github, gitinfo, hook, learn, profile


def _repo_setup(args) -> tuple[Path, str, str | None, dict | None]:
    root = gitinfo.repo_root(Path.cwd())
    repo, remote = check.pick_upstream(root, args.repo)
    prof = profile.load(repo.split("/")[0])
    return root, repo, remote, prof


def cmd_learn(args) -> int:
    try:
        prof = learn.learn(args.org, args.only, args.sample, args.include_forks)
    except github.GitHubError as e:
        print(f"preflight: {e}", file=sys.stderr)
        return 2
    path = profile.save(prof, Path(args.out) if args.out else None)
    _print_summary(prof)
    print(f"\nProfile written to {path}")
    return 0


def _print_summary(prof: dict) -> None:
    s = learn.summarize(prof)
    print(f"\n{prof['org']}: {s['repos']} repos, {s['pr_jobs']} jobs run on pull requests")
    order = ["run", "partial", "docker", "other-os", "secrets", "pr-metadata", "needs-docker", "timing", "incomplete", "reusable", "dynamic", "no-code"]
    for v in order:
        if s["verdicts"].get(v):
            print(f"  {v:<9} {s['verdicts'][v]}")
    if s["coverage"] is not None:
        print(f"Local coverage: {s['coverage']:.0%} of code-checking PR jobs can run on {prof['learned_on']}")
    if s["other_ci"]:
        print("Other CI systems: " + ", ".join(f"{k} ({v} repos)" for k, v in s["other_ci"].items()))
    if s["top_failures"]:
        print("Most failed jobs on recent PRs:")
        for n, key in s["top_failures"]:
            print(f"  {n:>4}  {key}")


def cmd_show(args) -> int:
    prof = profile.load(args.org)
    if not prof:
        print(f"preflight: no profile for {args.org}; run `preflight learn {args.org}`", file=sys.stderr)
        return 2
    _print_summary(prof)
    return 0


def _plan(args, hook_mode: bool):
    root, repo, remote, prof = _repo_setup(args)
    base = check.resolve_base(root, remote, prof, repo, args.base)
    changed = gitinfo.changed_files(root, base, include_worktree=not hook_mode)
    if not prof:
        print(f"(no profile for {repo.split('/')[0]}: checks run in file order; "
              f"`preflight learn {repo.split('/')[0]}` ranks them by past failures)")
    if args.docker and not docker.available():
        raise SystemExit("preflight: --docker needs a running Docker daemon (`docker info` failed)")
    plan = check.build_plan(root, repo, base, changed, prof, args.only, args.quick, docker=args.docker)
    return root, plan


def cmd_plan(args) -> int:
    _, plan = _plan(args, hook_mode=False)
    check.print_plan(plan)
    return 0


def cmd_check(args) -> int:
    root, plan = _plan(args, hook_mode=args.hook)
    check.print_plan(plan)
    if not plan.to_run:
        print("\nNothing to run locally for this change.")
        return 0
    print("\nRunning:")
    results = check.execute(plan, root, fail_fast=not args.keep_going, verbose=args.verbose,
                            snapshot="head" if args.hook else "worktree",
                            jobs=args.jobs or (docker.default_parallel() if args.docker else 1))
    report = Path(gitinfo.git(root, "rev-parse", "--absolute-git-dir")) / "preflight-report.md"
    check.write_report(plan, results, report, root)
    # Killed for memory even alone: this machine cannot judge it; GitHub's runner has 16 GB.
    no_memory = [r for r in results if r.status == "failed" and r.oom]
    failed = [r for r in results if r.status == "failed" and not r.oom]
    cancelled = [r for r in results if r.status == "cancelled"]
    print()
    if no_memory:
        print(f"! {len(no_memory)} job run(s) ran out of Docker memory even alone, so they were not checked here "
              f"(GitHub will run them; more Docker memory lets them run): "
              + ", ".join(f"{r.workflow} / {r.label}" for r in no_memory))
    if failed or cancelled:
        extra = f" ({len(cancelled)} cancelled after it)" if cancelled else ""
        print(f"✗ {len(failed)} of {len(results)} job runs failed{extra}. Fix them before pushing.")
        if args.hook:
            print("  Push blocked. To push anyway: PREFLIGHT_SKIP=1 git push")
        return 1
    ran = sum(len(p.combos) for p in plan.to_run)
    note = "" if len(results) == ran else f" ({ran - len(results)} not run)"
    checked = len(results) - len(no_memory)
    print(f"✓ All {checked} checked job runs passed locally{note}. "
          f"{len(plan.skipped)} jobs are left to GitHub.")
    print(f"  Report for your PR description: {report}")
    return 0


def cmd_bench(args) -> int:
    repo = args.repo
    if not repo:
        try:
            repo, _ = check.pick_upstream(gitinfo.repo_root(Path.cwd()), None)
        except (gitinfo.GitError, SystemExit):
            raise SystemExit("preflight: pass --repo owner/name (or run inside a checkout)")
    out = bench.bench_dir(repo)
    if args.reset:
        for name in ("cases.json", "results.jsonl"):
            (out / name).unlink(missing_ok=True)
    if not args.report:
        if not docker.available():
            raise SystemExit("preflight: bench runs jobs in Docker; `docker info` failed")
        try:
            if args.replan:
                branch = args.base or github.api(f"repos/{repo}")["default_branch"]
                print(f"Re-planned saved cases: {bench.replan(repo, branch)} job runs are now left to GitHub.")
            elif args.replay or args.add_failures:
                branch = args.base or github.api(f"repos/{repo}")["default_branch"]
                n = args.jobs or docker.default_parallel()
                if args.replay:
                    bench.replay_disagreements(repo, n, branch)
                if args.add_failures:
                    bench.add_failures(repo, args.add_failures, args.max_minutes, n, branch)
            else:
                bench.run(repo, args.cases, args.failing, args.per_case, args.max_minutes,
                          args.jobs or docker.default_parallel(), args.only, args.seed, args.base,
                          args.rerun_disagreements)
        except github.GitHubError as e:
            print(f"preflight: {e}", file=sys.stderr)
            return 2
    if not (out / "results.jsonl").exists():
        print("No results yet.")
        return 0
    text = bench.report(repo)
    (out / "report.md").write_text(text)
    print("\n" + text)
    print(f"Report: {out / 'report.md'}")
    return 0


def cmd_install_hook(args) -> int:
    root = gitinfo.repo_root(Path.cwd())
    extra = (["--quick"] if args.quick else []) + (["--docker"] if args.docker else []) \
        + ([f"--jobs={args.jobs}"] if args.jobs else []) \
        + (["--repo", args.repo] if args.repo else [])
    path = hook.install(root, extra, args.force)
    print(f"Installed {path}. `git push` now runs `preflight check` first.")
    return 0


def cmd_uninstall_hook(args) -> int:
    root = gitinfo.repo_root(Path.cwd())
    print("Removed the pre-push hook." if hook.uninstall(root) else "No preflight hook installed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="preflight", description="Run your org's CI locally before you push.")
    p.add_argument("--version", action="version", version=f"preflight {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("learn", help="scan a GitHub org and write its profile")
    s.add_argument("org")
    s.add_argument("--only", nargs="+", metavar="REPO", help="only these repos")
    s.add_argument("--sample", type=int, default=30, help="failed PR runs to sample per repo (0 = skip)")
    s.add_argument("--include-forks", action="store_true")
    s.add_argument("--out", help="write the profile here instead of ~/.preflight/profiles")
    s.set_defaults(fn=cmd_learn)

    s = sub.add_parser("show", help="summarize a learned org profile")
    s.add_argument("org")
    s.set_defaults(fn=cmd_show)

    for name, fn, helptext in (
        ("plan", cmd_plan, "list the checks a PR from this branch triggers, without running them"),
        ("check", cmd_check, "run those checks locally"),
    ):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--repo", help="owner/name the PR targets (default: upstream or origin remote)")
        s.add_argument("--base", help="base ref to compare with (default: <remote>/<default branch>)")
        s.add_argument("--only", nargs="+", metavar="JOB", help="only jobs or workflows with these names")
        s.add_argument("--quick", action="store_true", help="first matrix entry of each job only")
        s.add_argument("--docker", action="store_true",
                       help="run each job in a fresh Ubuntu container (also runs service and container jobs)")
        if name == "check":
            s.add_argument("--keep-going", action="store_true", help="run every job even after a failure")
            s.add_argument("--jobs", "-j", type=int, metavar="N",
                           help="job runs at a time in Docker mode (default: fits Docker's memory, at most 4)")
            s.add_argument("--verbose", "-v", action="store_true", help="stream step output")
            s.add_argument("--hook", action="store_true", help=argparse.SUPPRESS)
        s.set_defaults(fn=fn)

    s = sub.add_parser("bench", help="measure how often local results match GitHub's on recent PRs")
    s.add_argument("--repo", help="owner/name (default: this checkout's upstream)")
    s.add_argument("--cases", type=int, default=20, help="PR commits to replay (default 20)")
    s.add_argument("--failing", type=float, default=0.5, help="share of cases with a failed GitHub run (default 0.5)")
    s.add_argument("--per-case", type=int, default=6,
                   help="job runs per case: every job GitHub failed, topped up with random passed ones (0 = all)")
    s.add_argument("--max-minutes", type=float, default=15, help="skip jobs that took longer on GitHub (0 = no limit)")
    s.add_argument("--only", nargs="+", metavar="JOB", help="only jobs or workflows with these names")
    s.add_argument("--jobs", "-j", type=int, metavar="N")
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--base", help="base branch (default: the repo's default branch)")
    s.add_argument("--report", action="store_true", help="only print the report from results so far")
    s.add_argument("--reset", action="store_true", help="forget earlier cases and results")
    s.add_argument("--replan", action="store_true",
                   help="re-plan saved cases with this PreFlight (no jobs run); jobs now left to GitHub leave the comparison")
    s.add_argument("--replay", action="store_true", help="run again only the job runs that disagreed")
    s.add_argument("--add-failures", type=int, metavar="N",
                   help="add N PR commits with a failed job PreFlight can run, running only those jobs")
    s.add_argument("--rerun-disagreements", action="store_true",
                   help="replay the cases that disagreed with GitHub (after a fix)")
    s.set_defaults(fn=cmd_bench)

    s = sub.add_parser("install-hook", help="run `preflight check` before every git push")
    s.add_argument("--quick", action="store_true")
    s.add_argument("--docker", action="store_true")
    s.add_argument("--jobs", "-j", type=int, metavar="N")
    s.add_argument("--repo")
    s.add_argument("--force", action="store_true", help="replace an existing pre-push hook")
    s.set_defaults(fn=cmd_install_hook)

    s = sub.add_parser("uninstall-hook", help="remove the pre-push hook")
    s.set_defaults(fn=cmd_uninstall_hook)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except gitinfo.GitError as e:
        print(f"preflight: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
