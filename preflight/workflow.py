"""Parse GitHub Actions workflow files and classify each job.

The classification answers one question per job: can PreFlight run this on
the contributor's machine, and if not, why not.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import actions

PR_EVENTS = ("pull_request", "pull_request_target")
_SECRET_RE = re.compile(r"secrets\.([A-Za-z0-9_]+)")

# Job verdicts, best first.
RUN = "run"              # every step can run here
PARTIAL = "partial"      # runs, but some steps are skipped
DOCKER = "docker"        # needs a job container or service containers (phase 2)
OTHER_OS = "other-os"    # needs an OS this machine is not
SECRETS = "secrets"      # needs repository secrets
REUSABLE = "reusable"    # calls a reusable workflow (phase 2)
NO_CODE = "no-code"      # no run steps: a bot, labeler or upload-only job
DYNAMIC = "dynamic"      # matrix or runner computed at run time
PR_META = "pr-metadata"  # reads the PR itself (number, title, body), which exists only on GitHub
NEEDS_DOCKER = "needs-docker"  # a step starts Docker containers itself

_PR_META_RE = re.compile(
    r"github\.event\.(number|pull_request\.(number|title|body|labels|html_url))|github\.event\.pull_request\b(?!\.(head|base|draft))"
)

RUNNABLE = (RUN, PARTIAL)


@dataclass
class Step:
    index: int
    name: str
    id: str | None
    run: str | None
    uses: str | None
    with_: dict[str, Any]
    env: dict[str, Any]
    if_: Any
    shell: str | None
    working_directory: str | None
    continue_on_error: Any
    timeout_minutes: Any


@dataclass
class Job:
    id: str
    name: str
    runs_on: Any
    needs: list[str]
    if_: Any
    matrix: Any
    fail_fast: Any
    container: Any
    services: dict[str, Any]
    env: dict[str, Any]
    defaults_run: dict[str, Any]
    steps: list[Step]
    uses: str | None
    timeout_minutes: Any
    verdict: str = RUN
    reasons: list[str] = field(default_factory=list)


@dataclass
class Workflow:
    path: str
    name: str
    on: dict[str, Any]
    env: dict[str, Any]
    defaults_run: dict[str, Any]
    jobs: list[Job]

    @property
    def pr_event(self) -> str | None:
        for e in PR_EVENTS:
            if e in self.on:
                return e
        return None

    def pr_filters(self) -> tuple[list[str] | None, list[str] | None]:
        event = self.pr_event
        cfg = self.on.get(event) if event else None
        if not isinstance(cfg, dict):
            return None, None
        return _as_list(cfg.get("paths")), _as_list(cfg.get("paths-ignore"))

    def pr_types(self) -> list[str] | None:
        event = self.pr_event
        cfg = self.on.get(event) if event else None
        return _as_list(cfg.get("types")) if isinstance(cfg, dict) else None


def _as_list(v: Any) -> list[str] | None:
    if v is None:
        return None
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def _normalize_on(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        return {raw: None}
    if isinstance(raw, list):
        return {str(e): None for e in raw}
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items()}
    return {}


def parse(text: str, path: str) -> Workflow:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a workflow mapping")
    # YAML 1.1 reads the bare key `on` as the boolean True.
    on = data.get("on", data.get(True))
    wf = Workflow(
        path=path,
        name=str(data.get("name") or Path(path).stem),
        on=_normalize_on(on),
        env=dict(data.get("env") or {}),
        defaults_run=dict((data.get("defaults") or {}).get("run") or {}),
        jobs=[],
    )
    for job_id, raw in (data.get("jobs") or {}).items():
        raw = raw or {}
        strategy = raw.get("strategy") or {}
        needs = raw.get("needs") or []
        steps = [step_from_raw(i, st) for i, st in enumerate(raw.get("steps") or [])]
        wf.jobs.append(
            Job(
                id=str(job_id),
                name=str(raw.get("name") or job_id),
                runs_on=raw.get("runs-on"),
                needs=[needs] if isinstance(needs, str) else list(needs),
                if_=raw.get("if"),
                matrix=strategy.get("matrix"),
                fail_fast=strategy.get("fail-fast", True),
                container=raw.get("container"),
                services=dict(raw.get("services") or {}),
                env=dict(raw.get("env") or {}),
                defaults_run=dict((raw.get("defaults") or {}).get("run") or {}),
                steps=steps,
                uses=raw.get("uses"),
                timeout_minutes=raw.get("timeout-minutes"),
            )
        )
    return wf


def step_from_raw(i: int, s: Any) -> Step:
    s = s or {}
    return Step(
        index=i,
        name=str(s.get("name") or str(s.get("run") or "").split("\n")[0][:60] or s.get("uses") or f"step {i + 1}"),
        id=s.get("id"),
        run=s.get("run"),
        uses=s.get("uses"),
        with_=dict(s.get("with") or {}),
        env=dict(s.get("env") or {}),
        if_=s.get("if"),
        shell=s.get("shell"),
        working_directory=s.get("working-directory"),
        continue_on_error=s.get("continue-on-error", False),
        timeout_minutes=s.get("timeout-minutes"),
    )


def runner_os(runs_on: Any) -> str | None:
    """Map a ``runs-on`` value to Linux, macOS or Windows (None if unknown)."""
    labels = runs_on
    if isinstance(runs_on, dict):
        labels = runs_on.get("labels") or runs_on.get("group")
    if isinstance(labels, str):
        labels = [labels]
    text = " ".join(str(l) for l in labels or []).lower()
    if "${{" in text:
        return None
    if "windows" in text:
        return "Windows"
    if "macos" in text or "mac" in text.split("-"):
        return "macOS"
    if "ubuntu" in text or "linux" in text:
        return "Linux"
    if "self-hosted" in text:
        return None
    return "Linux"


def classify(job: Job, host_os: str, docker: bool = False, root: Path | None = None) -> None:
    """Set ``job.verdict`` and ``job.reasons`` for a machine running ``host_os``.

    With ``docker``, Linux jobs run in containers, so job containers and
    service containers are runnable and the host OS does not matter for them.
    """
    if docker:
        host_os = "Linux"
    reasons: list[str] = []
    if job.uses:
        job.verdict, job.reasons = REUSABLE, [f"calls reusable workflow {job.uses}"]
        return
    # Code steps: run steps, plus (in Docker mode) actions that PreFlight runs for real.
    run_steps = [s for s in job.steps if s.run or (docker and s.uses and actions.classify(s.uses)[0] == "unknown")]
    if not run_steps:
        job.verdict, job.reasons = NO_CODE, ["no run steps"]
        return
    if isinstance(job.matrix, str):
        job.verdict, job.reasons = DYNAMIC, ["matrix is computed at run time"]
        return
    os_name = runner_os(job.runs_on)
    if os_name is None:
        job.verdict, job.reasons = DYNAMIC, [f"runner {job.runs_on!r} is computed or self-hosted"]
        return
    if os_name == "Windows" and host_os != "Windows":
        job.verdict, job.reasons = OTHER_OS, ["needs a Windows runner"]
        return
    if os_name == "macOS" and host_os != "macOS":
        job.verdict, job.reasons = OTHER_OS, ["needs a macOS runner"]
        return
    if (job.container or job.services) and not docker:
        job.verdict, job.reasons = DOCKER, ["uses a job container or service containers"]
        return
    secrets = set()
    pr_meta = False
    for s in job.steps:
        blob = yaml.safe_dump({"run": s.run, "with": s.with_, "env": s.env})
        secrets |= set(_SECRET_RE.findall(blob))
        pr_meta = pr_meta or bool(_PR_META_RE.search(blob))
    if pr_meta:
        job.verdict, job.reasons = PR_META, ["reads the pull request itself (number or description); runs once the PR is open"]
        return
    for s in job.steps:
        if s.uses and actions.classify(s.uses)[0] == "needs-docker":
            job.verdict, job.reasons = NEEDS_DOCKER, [f"step '{s.name}' {actions.classify(s.uses)[1]} inside the job"]
            return
    secrets |= set(_SECRET_RE.findall(yaml.safe_dump(job.env)))
    secrets.discard("GITHUB_TOKEN")
    if secrets:
        job.verdict, job.reasons = SECRETS, ["needs secrets: " + ", ".join(sorted(secrets))]
        return
    for s in job.steps:
        if s.uses:
            kind, detail = actions.classify(s.uses)
            if kind == "unknown" and docker:
                why = _docker_action_gap(s.uses, root)
                if why:
                    reasons.append(f"step '{s.name}' skipped: {why}")
            elif kind == "unknown":
                reasons.append(f"step '{s.name}' skipped: {s.uses} {detail} (--docker runs it)")
    if os_name == "Linux" and host_os != "Linux":
        reasons.append(f"Linux job run on {host_os}; GNU vs BSD tool differences possible")
    job.verdict = PARTIAL if any("skipped" in r for r in reasons) else RUN
    job.reasons = reasons


def _docker_action_gap(uses: str, root: Path | None) -> str | None:
    """Why Docker mode cannot run this action, or None when it can."""
    from . import actionfetch  # here to keep workflow parsing free of network code
    if uses.startswith("docker://"):
        return f"{uses} is a docker action (not run yet)"
    if uses.startswith("./") and root is None:
        return None
    try:
        action = actionfetch.resolve(uses, root or Path("."))
    except (actionfetch.FetchError, OSError) as e:
        return f"could not read {uses}: {e}"
    if action.kind in ("node", "composite"):
        return None
    if action.kind == "docker":
        return f"{uses} is a docker container action (not run yet)"
    return f"{uses} uses '{action.using}', which is not supported"


def expand_matrix(matrix: Any) -> list[dict[str, Any]]:
    """Expand ``strategy.matrix`` into combinations, following GitHub's rules."""
    if not matrix or not isinstance(matrix, dict):
        return [{}]
    include = matrix.get("include") or []
    exclude = matrix.get("exclude") or []
    axes = {k: v for k, v in matrix.items() if k not in ("include", "exclude")}
    keys = list(axes)
    combos: list[dict[str, Any]] = []
    if keys:
        values = [v if isinstance(v, list) else [v] for v in axes.values()]
        combos = [dict(zip(keys, prod)) for prod in itertools.product(*values)]
    combos = [
        c for c in combos
        if not any(all(c.get(k) == v for k, v in ex.items()) for ex in exclude)
    ]
    originals = [dict(c) for c in combos]
    for inc in include:
        added = False
        for c, orig in zip(combos, originals):
            # An include extends a combination when it does not overwrite
            # any of that combination's original matrix values.
            if all(k not in orig or orig[k] == v for k, v in inc.items()):
                c.update({k: v for k, v in inc.items() if k not in orig})
                added = True
        if not added:
            combos.append(dict(inc))
    return combos or [{}]


def load_dir(workflows_dir: Path) -> list[Workflow]:
    out = []
    if not workflows_dir.is_dir():
        return out
    for f in sorted(workflows_dir.iterdir()):
        if f.suffix in (".yml", ".yaml"):
            try:
                out.append(parse(f.read_text(), str(f)))
            except Exception as e:  # a broken file should not stop the rest
                print(f"preflight: could not parse {f}: {e}")
    return out
