"""Run one job of a workflow on this machine, step by step, like the Actions runner.

Supported: ``run:`` steps with bash/sh/python shells, step/job/workflow ``env``,
``working-directory`` defaults, ``if:`` conditions with success/failure state,
``continue-on-error``, timeouts, and the ``GITHUB_ENV`` / ``GITHUB_PATH`` /
``GITHUB_OUTPUT`` files that pass values between steps.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import actions, expr
from .workflow import Job, Step, Workflow

# Steps that set up a fresh CI machine. On a contributor's machine the tools
# should already be installed, and these commands would change the system.
_SETUP_RE = re.compile(
    r"(^|[;&|]\s*|\n\s*)(sudo\b|apt(-get)?\s+(install|update)|brew\s+install|choco\s+install|"
    r"snap\s+install|yum\s+install|dnf\s+install|sdk\s+install)|get\.sdkman\.io",
)

SHELLS = {
    None: ["bash", "--noprofile", "--norc", "-eo", "pipefail", "{0}"],
    "bash": ["bash", "--noprofile", "--norc", "-eo", "pipefail", "{0}"],
    "sh": ["sh", "-e", "{0}"],
    "python": ["python3", "{0}"],
    "pwsh": ["pwsh", "-command", ". '{0}'"],
}


@dataclass
class StepResult:
    name: str
    status: str  # passed | failed | skipped
    detail: str = ""
    seconds: float = 0.0


@dataclass
class JobResult:
    workflow: str
    job_id: str
    label: str
    status: str  # passed | failed
    steps: list[StepResult] = field(default_factory=list)
    log_path: Path | None = None
    seconds: float = 0.0

    @property
    def failed_step(self) -> StepResult | None:
        return next((s for s in self.steps if s.status == "failed"), None)


def job_label(job: Job, combo: dict[str, Any]) -> str:
    if not combo:
        return job.name if "${{" not in job.name else job.id
    vals = ", ".join(expr.to_str(v) for v in combo.values())
    return f"{job.id} ({vals})"


def _parse_env_file(path: Path) -> dict[str, str]:
    return _parse_env_text(path.read_text()) if path.exists() else {}


def _parse_env_text(text: str) -> dict[str, str]:
    """Read ``KEY=value`` and ``KEY<<DELIM ... DELIM`` entries."""
    out: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if "<<" in line and ("=" not in line or line.index("<<") < line.index("=")):
            key, delim = line.split("<<", 1)
            buf = []
            i += 1
            while i < len(lines) and lines[i] != delim:
                buf.append(lines[i])
                i += 1
            out[key.strip()] = "\n".join(buf)
        elif "=" in line:
            key, val = line.split("=", 1)
            out[key.strip()] = val
        i += 1
    return out


def _tool_present(tool: str) -> bool:
    return shutil.which(tool) is not None


def _check_setup_action(step: Step, tool: str) -> tuple[bool, str]:
    if not _tool_present(tool):
        want = step.with_.get("java-version") or step.with_.get("node-version") or step.with_.get("python-version")
        hint = f" (the workflow asks for {want})" if want else ""
        return False, f"{tool} is not installed{hint}"
    if tool == "java" and step.with_.get("java-version"):
        want = str(step.with_["java-version"]).split(".")[0]
        proc = subprocess.run(["java", "-version"], capture_output=True, text=True)
        m = re.search(r'version "(\d+)', proc.stderr + proc.stdout)
        if m and want.isdigit() and m.group(1) != want:
            return True, f"java {m.group(1)} found, workflow uses {want}; results may differ"
    return True, f"{tool} found"


def run_job(
    wf: Workflow,
    job: Job,
    combo: dict[str, Any],
    base_values: dict[str, Any],
    root: Path,
    log_path: Path,
    runner_os: str,
    echo: bool = False,
    box: Any = None,
) -> JobResult:
    """Run one matrix combination of ``job``.

    ``box`` is a started :class:`preflight.docker.DockerJob`; without it the
    steps run directly on this machine in ``root``.
    """
    started = time.time()
    label = job_label(job, combo)
    result = JobResult(workflow=wf.name, job_id=job.id, label=label, status="passed", log_path=log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    host_temp = Path(tempfile.mkdtemp(prefix="preflight-"))
    if box:
        temp = box.temp
        workspace = box.workspace
        cmd_dir = f"{temp}/_runner_file_commands"
    else:
        temp = str(host_temp)
        workspace = str(root)
        cmd_dir = temp
    files = {k: f"{cmd_dir}/{k.lower()}" for k in ("GITHUB_ENV", "GITHUB_PATH", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY")}
    event_path = f"{temp}/event.json"
    event_json = json.dumps((base_values.get("github") or {}).get("event") or {})
    gh_values = dict(base_values.get("github") or {}, workspace=workspace)
    base_values = dict(base_values, github=gh_values)

    def write(path: str, content: str) -> None:
        if box:
            box.write_file(path, content)
        else:
            Path(path).write_text(content)

    def read_clear(path: str) -> str:
        if box:
            return box.read_and_clear(path)
        f = Path(path)
        if not f.exists():
            return ""
        text = f.read_text()
        f.write_text("")
        return text

    write(event_path, event_json)
    for f in files.values():
        write(f, "")

    extra_env: dict[str, str] = {}
    extra_path: list[str] = []
    step_values: dict[str, Any] = {}
    status = "success"
    arch = "ARM64" if os.uname().machine in ("arm64", "aarch64") else "X64"

    def ctx_for(env: dict[str, Any]) -> expr.Context:
        values = dict(base_values)
        values.update({
            "matrix": combo,
            "env": env,
            "steps": step_values,
            "runner": {"os": runner_os, "temp": temp, "arch": arch, "tool_cache": "/opt/hostedtoolcache"},
            "job": {"status": status, "services": box.services_context() if box else {}},
            "secrets": {},
            "inputs": {},
            "vars": {},
        })
        return expr.Context(values=values, status=status)

    def interp_map(m: dict[str, Any], env: dict[str, Any]) -> dict[str, str]:
        out = {}
        for k, v in m.items():
            out[str(k)] = expr.interpolate(expr.to_str(v), ctx_for(env))
        return out

    with log_path.open("w") as log:
        def say(msg: str) -> None:
            log.write(msg + "\n")
            log.flush()

        say(f"# {wf.name} / {label}" + (f"  [docker: {box.image}]" if box else ""))
        env: dict[str, Any] = {}
        env.update(interp_map(wf.env, env))
        env.update(interp_map(job.env, env))

        for step in job.steps:
            step_env = dict(env)
            step_env.update(extra_env)
            step_env.update(interp_map(step.env, step_env))
            ctx = ctx_for(step_env)
            try:
                should_run = expr.evaluate_condition(step.if_, ctx)
            except expr.ExpressionError as e:
                should_run = status == "success"
                say(f"! could not read `if:` of '{step.name}' ({e}); treating it as success()")
            if not should_run:
                result.steps.append(StepResult(step.name, "skipped", "condition false"))
                continue

            t0 = time.time()
            if step.uses:
                kind, detail = actions.classify(step.uses)
                if kind == "tool":
                    if box:
                        with_ = interp_map(step.with_, step_env)
                        say(f"== {step.name}: setting up")
                        ok, msg, add_env, add_path = box.setup_tool(actions.action_name(step.uses), with_, detail)
                        extra_env.update(add_env)
                        for p in reversed(add_path):
                            if p not in extra_path:
                                extra_path.insert(0, p)
                    else:
                        ok, msg = _check_setup_action(step, detail)
                    say(f"== {step.name}: {msg}")
                    if not ok:
                        result.steps.append(StepResult(step.name, "failed", msg, time.time() - t0))
                        status = "failure"
                    else:
                        result.steps.append(StepResult(step.name, "passed", msg, time.time() - t0))
                else:
                    say(f"== {step.name}: skipped ({detail})")
                    result.steps.append(StepResult(step.name, "skipped", f"{step.uses}: {detail}"))
                continue

            if step.run is None:
                result.steps.append(StepResult(step.name, "skipped", "nothing to run"))
                continue

            try:
                script = expr.interpolate(step.run, ctx)
            except expr.ExpressionError as e:
                result.steps.append(StepResult(step.name, "failed", f"bad expression: {e}"))
                status = "failure"
                continue

            if not box and _SETUP_RE.search(script):
                say(f"== {step.name}: skipped (machine setup; uses tools already installed here)")
                result.steps.append(StepResult(step.name, "skipped", "machine setup step"))
                continue

            shell = step.shell or job.defaults_run.get("shell") or wf.defaults_run.get("shell")
            if shell is None and box and not box.has_bash:
                shell = "sh"
            template = SHELLS.get(shell)
            if template is None:
                template = shell.split() + (["{0}"] if "{0}" not in shell else [])
            suffix = ".py" if shell == "python" else ".sh"
            script_file = f"{temp}/step{step.index}{suffix}"
            write(script_file, script)
            argv = [a.replace("{0}", script_file) for a in template]

            wd = step.working_directory or job.defaults_run.get("working-directory") or wf.defaults_run.get("working-directory")
            wd = expr.interpolate(str(wd), ctx) if wd else None
            if box:
                cwd = posixpath.join(workspace, wd) if wd else workspace
            else:
                cwd = str(root / wd) if wd else str(root)

            write(files["GITHUB_OUTPUT"], "")
            if box:
                proc_env = {}
            else:
                proc_env = os.environ.copy()
                proc_env.pop("GITHUB_TOKEN", None)
            proc_env.update({k: expr.to_str(v) for k, v in step_env.items()})
            proc_env.update(_github_env(base_values, workspace, runner_os, temp, event_path))
            proc_env.update(files)
            base_path = box.default_path if box else proc_env.get("PATH", "")
            if extra_path or box:
                proc_env["PATH"] = ":".join(extra_path + [base_path])

            timeout = step.timeout_minutes or job.timeout_minutes or 360
            say(f"== {step.name}\n$ {script.strip()}")
            try:
                if box:
                    proc = box.popen(argv, cwd, proc_env)
                else:
                    proc = subprocess.Popen(
                        argv, cwd=cwd, env=proc_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
                    )
                assert proc.stdout is not None
                for line in proc.stdout:
                    log.write(line)
                    if echo:
                        print("    " + line, end="")
                code = proc.wait(timeout=float(timeout) * 60)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = -1
                say(f"! timed out after {timeout} minutes")
            except FileNotFoundError as e:
                code = 127
                say(f"! {e}")

            seconds = time.time() - t0
            # Values this step passed on to later steps.
            extra_env.update(_parse_env_text(read_clear(files["GITHUB_ENV"])))
            for p in read_clear(files["GITHUB_PATH"]).splitlines():
                if p.strip() and p.strip() not in extra_path:
                    extra_path.insert(0, p.strip())
            outputs = _parse_env_text(read_clear(files["GITHUB_OUTPUT"]))
            if step.id:
                step_values[step.id] = {
                    "outputs": outputs,
                    "outcome": "success" if code == 0 else "failure",
                    "conclusion": "success" if code == 0 or _truthy(step.continue_on_error, ctx) else "failure",
                }
            if code == 0:
                result.steps.append(StepResult(step.name, "passed", seconds=seconds))
            elif _truthy(step.continue_on_error, ctx):
                result.steps.append(StepResult(step.name, "passed", f"exit {code}, continue-on-error", seconds))
            else:
                detail = f"exit {code}"
                if code == 127:
                    log.flush()
                    missing = re.findall(r"(?:line \d+: )?([\w.+-]+): (?:command )?not found", log_path.read_text(errors="replace"))
                    if missing:
                        tool = missing[-1]
                        if box:
                            detail = f"missing tool '{tool}' in the container image"
                        else:
                            hint = f"brew install {tool}" if runner_os == "macOS" else f"install {tool}"
                            detail = f"missing tool '{tool}' (CI installs it on its own machine; run: {hint})"
                result.steps.append(StepResult(step.name, "failed", detail, seconds))
                status = "failure"

    shutil.rmtree(host_temp, ignore_errors=True)
    result.status = "passed" if status == "success" else "failed"
    result.seconds = time.time() - started
    return result


def _truthy(value: Any, ctx: expr.Context) -> bool:
    if isinstance(value, str) and "${{" in value:
        return expr.to_bool(expr.interpolate(value, ctx) == "true")
    return expr.to_bool(value)


def _github_env(values: dict[str, Any], workspace: str, runner_os: str, temp: str, event_path: str) -> dict[str, str]:
    gh = values.get("github") or {}
    return {
        "CI": "true",
        "GITHUB_ACTIONS": "true",
        "GITHUB_WORKSPACE": workspace,
        "GITHUB_REPOSITORY": expr.to_str(gh.get("repository")),
        "GITHUB_REPOSITORY_OWNER": expr.to_str(gh.get("repository_owner")),
        "GITHUB_EVENT_NAME": expr.to_str(gh.get("event_name")),
        "GITHUB_EVENT_PATH": event_path,
        "GITHUB_REF": expr.to_str(gh.get("ref")),
        "GITHUB_SHA": expr.to_str(gh.get("sha")),
        "GITHUB_BASE_REF": expr.to_str(gh.get("base_ref")),
        "GITHUB_HEAD_REF": expr.to_str(gh.get("head_ref")),
        "GITHUB_ACTOR": expr.to_str(gh.get("actor")),
        "GITHUB_RUN_ID": "0",
        "GITHUB_RUN_NUMBER": "0",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_API_URL": "https://api.github.com",
        "RUNNER_OS": runner_os,
        "RUNNER_TEMP": temp,
        "RUNNER_ARCH": "ARM64" if os.uname().machine in ("arm64", "aarch64") else "X64",
        "PREFLIGHT": "1",
    }
