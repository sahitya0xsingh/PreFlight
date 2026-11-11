"""Command line entry point: ``preflight learn | show | plan | check | install-hook``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, check, docker, github, gitinfo, hook, learn, profile


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
    order = ["run", "partial", "docker", "other-os", "secrets", "pr-metadata", "reusable", "dynamic", "no-code"]
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
    failed = [r for r in results if r.status == "failed"]
    cancelled = [r for r in results if r.status == "cancelled"]
    print()
    if failed or cancelled:
        extra = f" ({len(cancelled)} cancelled after it)" if cancelled else ""
        print(f"✗ {len(failed)} of {len(results)} job runs failed{extra}. Fix them before pushing.")
        if args.hook:
            print("  Push blocked. To push anyway: PREFLIGHT_SKIP=1 git push")
        return 1
    ran = sum(len(p.combos) for p in plan.to_run)
    note = "" if len(results) == ran else f" ({ran - len(results)} not run)"
    print(f"✓ All {len(results)} job runs passed locally{note}. "
          f"{len(plan.skipped)} jobs are left to GitHub.")
    print(f"  Report for your PR description: {report}")
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
