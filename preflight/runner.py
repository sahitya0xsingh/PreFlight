"""Run one job of a workflow, step by step, like the Actions runner.

Supported: ``run:`` steps with bash/sh/python shells, step/job/workflow ``env``,
``working-directory`` defaults, ``if:`` conditions with success/failure state,
``continue-on-error``, timeouts, the ``GITHUB_ENV`` / ``GITHUB_PATH`` /
``GITHUB_OUTPUT`` / ``GITHUB_STATE`` files, and the ``::set-output`` style
workflow commands.

In Docker mode, marketplace actions PreFlight does not emulate are run for
real: JavaScript actions (with ``pre`` and ``post``), composite actions and
local ``./`` actions. Native mode skips them, as it runs on the
contributor's own machine.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import actionfetch, actions, expr
from . import docker as dockermod
from .workflow import Job, Step, Workflow, step_from_raw

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

# A failed third-party action whose log says this was refused access to
# GitHub: local runs get no token, so this is not the code's fault.
_NEEDS_GITHUB = re.compile(
    r"Bad credentials|Authentication failed|HTTP 401|401 Unauthorized|Resource not accessible by integration|Requires authentication|"
    r"Parameter token or opts\.auth is required|Input required and not supplied: (github[-_])?token|"
    r"API rate limit exceeded|HttpError: (Not Found|Unauthorized|Forbidden)|status(Code)?: 40[13]",
    re.I,
)
_COMMAND = re.compile(r"^::([\w-]+)(?: ([^:]*))?::(.*)$")
MAX_ACTION_DEPTH = 10


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
    status: str  # passed | failed | cancelled | not-checked (this machine cannot run it)
    steps: list[StepResult] = field(default_factory=list)
    log_path: Path | None = None
    seconds: float = 0.0
    oom: bool = False      # the kernel killed a process in it for want of memory
    note: str = ""         # e.g. failed on ARM64 only, passed under x64 like GitHub's runners

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


def _input_env(name: str) -> str:
    return "INPUT_" + name.replace(" ", "_").upper()


_UPLOAD_SH = r'''
set -e
shopt -s globstar nullglob dotglob
files=()
while IFS= read -r p; do
  p="${p#"${p%%[![:space:]]*}"}"; p="${p%"${p##*[![:space:]]}"}"
  case "$p" in ''|'!'*) continue;; esac
  p="${p/#\~/$HOME}"
  for f in $p; do files+=("$(realpath -s "$f")"); done
done <<< "$A_PATH"
if [ ${#files[@]} -eq 0 ]; then
  msg="No files were found with the provided path: $(echo $A_PATH). No artifacts will be uploaded."
  case "$A_IF_NONE" in error) echo "Error: $msg"; exit 1;; ignore) echo "$msg (ignored)"; exit 0;; *) echo "Warning: $msg"; exit 0;; esac
fi
# Like the real action: paths are kept relative to the least common ancestor of what was matched.
if [ ${#files[@]} -eq 1 ] && [ -d "${files[0]}" ]; then root="${files[0]}"; else
  root=$(dirname "${files[0]}")
  for f in "${files[@]}"; do while [ "$root" != / ] && [ "${f#$root/}" = "$f" ]; do root=$(dirname "$root"); done; done
fi
dest="$ART/$A_NAME"; rm -rf "$dest"; mkdir -p "$dest"
for f in "${files[@]}"; do
  if [ "$f" = "$root" ]; then cp -a "$f/." "$dest/"; continue; fi
  rel="${f#$root/}"; mkdir -p "$dest/$(dirname "$rel")"; cp -a "$f" "$dest/$rel"
done
echo "uploaded artifact '$A_NAME' ($(find "$dest" -type f | wc -l | tr -d ' ') files, kept for this run's other jobs)"
echo "artifact-id=$A_NAME" >> "$GITHUB_OUTPUT"
'''

_DOWNLOAD_SH = r'''
set -e
dest="${A_PATH/#\~/$HOME}"; mkdir -p "$dest"
if [ -n "$A_NAME" ]; then
  [ -d "$ART/$A_NAME" ] || { echo "Error: Unable to download artifact(s): Artifact not found for name: $A_NAME"; exit 1; }
  cp -a "$ART/$A_NAME/." "$dest/"; n=1
else
  n=0
  for d in "$ART"/*/; do
    a=$(basename "$d")
    if [ -n "$A_PATTERN" ]; then case "$a" in $A_PATTERN) ;; *) continue;; esac; fi
    if [ "$A_MERGE" = true ]; then cp -a "$d." "$dest/"; else mkdir -p "$dest/$a"; cp -a "$d." "$dest/$a/"; fi
    n=$((n+1))
  done
fi
echo "download-path=$(realpath "$dest")" >> "$GITHUB_OUTPUT"
echo "downloaded $n artifact(s) to $A_PATH"
'''


@dataclass
class _Scope:
    """Where a step runs: the job itself, or inside a composite action."""
    steps: dict[str, Any]
    inputs: dict[str, str] = field(default_factory=dict)
    action: actionfetch.Action | None = None
    action_dir: str | None = None     # the action's directory where the steps run
    depth: int = 0
    prefix: str = ""                  # "composite-name / " for nested step names


@dataclass
class _Outcome:
    code: int
    outputs: dict[str, str] = field(default_factory=dict)
    state: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    log_text: str = ""


class _JobRun:
    def __init__(self, wf: Workflow, job: Job, combo: dict[str, Any], base_values: dict[str, Any], root: Path,
                 log_path: Path, runner_os: str, echo: bool, box: Any, cancel: Any, echo_prefix: str):
        self.wf, self.job, self.combo, self.root = wf, job, combo, root
        self.runner_os, self.echo, self.box, self.cancel, self.echo_prefix = runner_os, echo, box, cancel, echo_prefix
        self.log_path = log_path
        self.host_temp = Path(tempfile.mkdtemp(prefix="preflight-"))
        if box:
            self.temp, self.workspace = box.temp, box.workspace
            self.cmd_dir = f"{self.temp}/_runner_file_commands"
        else:
            self.temp, self.workspace = str(self.host_temp), str(root)
            self.cmd_dir = self.temp
        self.files = {k: f"{self.cmd_dir}/{k.lower()}"
                      for k in ("GITHUB_ENV", "GITHUB_PATH", "GITHUB_OUTPUT", "GITHUB_STATE", "GITHUB_STEP_SUMMARY")}
        self.event_path = f"{self.temp}/event.json"
        # Only an explicitly given token reaches jobs, and only in containers:
        # third-party action code runs with it.
        self.token = os.environ.get("PREFLIGHT_GITHUB_TOKEN", "") if box else ""
        gh_values = dict(base_values.get("github") or {}, workspace=self.workspace)
        if self.token:
            gh_values["token"] = self.token
        self.base_values = dict(base_values, github=gh_values)
        self.extra_env: dict[str, str] = {}
        self.extra_path: list[str] = []
        self.status = "success"
        self.arch = dockermod.arch()[0] if box else ("ARM64" if os.uname().machine in ("arm64", "aarch64") else "X64")
        self.posts: list[Callable[[], None]] = []
        self.counter = 0
        self.node_paths: dict[str, list[str]] = {}
        self.copied: dict[str, str] = {}
        self.log = None

    # -- small helpers

    def say(self, msg: str) -> None:
        self.log.write(msg + "\n")
        self.log.flush()

    def write(self, path: str, content: str) -> None:
        if self.box:
            self.box.write_file(path, content)
        else:
            Path(path).write_text(content)

    def read_clear(self, path: str) -> str:
        if self.box:
            return self.box.read_and_clear(path)
        f = Path(path)
        if not f.exists():
            return ""
        text = f.read_text()
        f.write_text("")
        return text

    def ctx(self, env: dict[str, Any], scope: _Scope) -> expr.Context:
        values = dict(self.base_values)
        values.update({
            "matrix": self.combo,
            "env": env,
            "steps": scope.steps,
            "runner": {"os": self.runner_os, "temp": self.temp, "arch": self.arch,
                       "tool_cache": "/opt/hostedtoolcache"},
            "job": {"status": self.status, "services": self.box.services_context() if self.box else {}},
            "secrets": {"GITHUB_TOKEN": self.token} if self.token else {},
            "inputs": scope.inputs,
            "vars": {},
        })
        if scope.action_dir:
            values["github"] = dict(values["github"], action_path=scope.action_dir)
        return expr.Context(values=values, status=self.status)

    def interp_map(self, m: dict[str, Any], env: dict[str, Any], scope: _Scope) -> dict[str, str]:
        return {str(k): expr.interpolate(expr.to_str(v), self.ctx(env, scope)) for k, v in m.items()}

    # -- running one process

    def run_process(self, argv: list[str], cwd: str, env: dict[str, str], timeout_min: Any,
                    extra: dict[str, str] | None = None) -> _Outcome:
        for k in ("GITHUB_OUTPUT", "GITHUB_STATE"):
            self.write(self.files[k], "")
        if self.box:
            proc_env: dict[str, str] = {}
        else:
            proc_env = os.environ.copy()
            proc_env.pop("GITHUB_TOKEN", None)
        proc_env.update({k: expr.to_str(v) for k, v in env.items()})
        proc_env.update(_github_env(self.base_values, self.workspace, self.runner_os, self.temp, self.event_path))
        proc_env.update(self.files)
        proc_env.update(extra or {})
        base_path = self.box.default_path if self.box else proc_env.get("PATH", "")
        if self.extra_path or self.box:
            proc_env["PATH"] = ":".join(self.extra_path + [base_path])

        out = _Outcome(code=0)
        self.log.flush()
        start = os.path.getsize(self.log_path)
        stop_token = None
        timeout = timeout_min or self.job.timeout_minutes or 360
        try:
            if self.box:
                proc = self.box.popen(argv, cwd, proc_env)
            else:
                proc = subprocess.Popen(argv, cwd=cwd, env=proc_env, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True)
            assert proc.stdout is not None
            for line in proc.stdout:
                self.log.write(line)
                if self.echo:
                    print(self.echo_prefix + line, end="", flush=True)
                m = _COMMAND.match(line.rstrip("\n"))
                if not m:
                    continue
                cmd, props, value = m.group(1), m.group(2) or "", m.group(3)
                if stop_token:
                    if cmd == stop_token:
                        stop_token = None
                    continue
                params = dict(p.split("=", 1) for p in props.split(",") if "=" in p)
                if cmd == "stop-commands":
                    stop_token = value
                elif cmd == "set-output" and "name" in params:
                    out.outputs[params["name"]] = value
                elif cmd == "save-state" and "name" in params:
                    out.state[params["name"]] = value
                elif cmd == "add-path":
                    self.extra_path.insert(0, value)
                elif cmd == "error":
                    out.errors.append(value.replace("%0A", " ").strip())
            out.code = proc.wait(timeout=float(timeout) * 60)
        except subprocess.TimeoutExpired:
            proc.kill()
            out.code = -1
            self.say(f"! timed out after {timeout} minutes")
        except FileNotFoundError as e:
            out.code = 127
            self.say(f"! {e}")
        self.log.flush()
        with open(self.log_path, "rb") as f:
            f.seek(start)
            out.log_text = f.read().decode(errors="replace")

        # Values this process passed on to later steps.
        self.extra_env.update(_parse_env_text(self.read_clear(self.files["GITHUB_ENV"])))
        for p in self.read_clear(self.files["GITHUB_PATH"]).splitlines():
            if p.strip() and p.strip() not in self.extra_path:
                self.extra_path.insert(0, p.strip())
        out.outputs.update(_parse_env_text(self.read_clear(self.files["GITHUB_OUTPUT"])))
        out.state.update(_parse_env_text(self.read_clear(self.files["GITHUB_STATE"])))
        return out

    def run_script(self, script: str, shell: str | None, cwd: str, env: dict[str, str], timeout: Any) -> _Outcome:
        if shell is None and self.box and not self.box.has_bash:
            shell = "sh"
        template = SHELLS.get(shell)
        if template is None:
            template = shell.split() + (["{0}"] if "{0}" not in shell else [])
        self.counter += 1
        script_file = f"{self.temp}/step{self.counter}{'.py' if shell == 'python' else '.sh'}"
        self.write(script_file, script)
        argv = [a.replace("{0}", script_file) for a in template]
        return self.run_process(argv, cwd, env, timeout)

    # -- steps

    def run_steps(self, steps: list[Step], env: dict[str, Any], scope: _Scope, results: list[StepResult]) -> None:
        for step in steps:
            if self.cancel is not None and self.cancel.is_set():
                results.append(StepResult(scope.prefix + step.name, "skipped", "cancelled"))
                continue
            results.append(self.run_step(step, env, scope))

    def run_step(self, step: Step, env: dict[str, Any], scope: _Scope) -> StepResult:
        name = scope.prefix + step.name
        step_env = dict(env)
        step_env.update(self.extra_env)
        step_env.update(self.interp_map(step.env, step_env, scope))
        ctx = self.ctx(step_env, scope)
        try:
            should_run = expr.evaluate_condition(step.if_, ctx)
        except expr.ExpressionError as e:
            should_run = self.status == "success"
            self.say(f"! could not read `if:` of '{name}' ({e}); treating it as success()")
        if not should_run:
            return StepResult(name, "skipped", "condition false")

        t0 = time.time()
        if step.uses:
            res, outputs, code = self.run_uses(step, step_env, scope, name)
        elif step.run is not None:
            res, outputs, code = self.run_run(step, step_env, scope, name, ctx)
        else:
            return StepResult(name, "skipped", "nothing to run")
        res.seconds = time.time() - t0

        ok = res.status != "failed"
        if res.status == "failed" and _truthy(step.continue_on_error, ctx):
            res = StepResult(name, "passed", f"{res.detail}, continue-on-error", res.seconds)
        if step.id:
            scope.steps[step.id] = {
                "outputs": outputs,
                "outcome": "success" if ok else "failure",
                "conclusion": "success" if res.status != "failed" else "failure",
            }
        if res.status == "failed":
            self.status = "failure"
        return res

    def run_run(self, step: Step, step_env: dict[str, Any], scope: _Scope, name: str,
                ctx: expr.Context) -> tuple[StepResult, dict[str, str], int]:
        try:
            script = expr.interpolate(step.run, ctx)
        except expr.ExpressionError as e:
            return StepResult(name, "failed", f"bad expression: {e}"), {}, 1
        if not self.box and _SETUP_RE.search(script):
            self.say(f"== {name}: skipped (machine setup; uses tools already installed here)")
            return StepResult(name, "skipped", "machine setup step"), {}, 0
        shell = step.shell or self.job.defaults_run.get("shell") or self.wf.defaults_run.get("shell")
        wd = step.working_directory
        if wd is None and scope.action is None:
            wd = self.job.defaults_run.get("working-directory") or self.wf.defaults_run.get("working-directory")
        wd = expr.interpolate(str(wd), ctx) if wd else None
        if self.box:
            cwd = posixpath.join(self.workspace, wd) if wd else self.workspace
        else:
            cwd = str(self.root / wd) if wd else str(self.root)
        self.say(f"== {name}\n$ {script.strip()}")
        env = dict(step_env)
        if scope.action_dir:
            env["GITHUB_ACTION_PATH"] = scope.action_dir
        out = self.run_script(script, shell, cwd, env, step.timeout_minutes)
        if out.code == 0:
            return StepResult(name, "passed"), out.outputs, 0
        return StepResult(name, "failed", self._failure_detail(out)), out.outputs, out.code

    def _failure_detail(self, out: _Outcome) -> str:
        detail = f"exit {out.code}"
        if out.code == 127:
            missing = re.findall(r"(?:line \d+: )?([\w.+-]+): (?:command )?not found", out.log_text)
            if missing:
                tool = missing[-1]
                if self.box:
                    return f"missing tool '{tool}' in the container image"
                hint = f"brew install {tool}" if self.runner_os == "macOS" else f"install {tool}"
                return f"missing tool '{tool}' (CI installs it on its own machine; run: {hint})"
        if out.errors:
            detail += f": {out.errors[0][:160]}"
        if "Cannot connect to the Docker daemon" in out.log_text:
            detail += " (the step uses Docker itself; job containers have no Docker daemon)"
        return detail

    def run_uses(self, step: Step, step_env: dict[str, Any], scope: _Scope,
                 name: str) -> tuple[StepResult, dict[str, str], int]:
        kind, detail = actions.classify(step.uses)
        if kind == "tool":
            if self.box:
                with_ = self.interp_map(step.with_, step_env, scope)
                self.say(f"== {name}: setting up")
                ok, msg, add_env, add_path = self.box.setup_tool(actions.action_name(step.uses), with_, detail)
                self.extra_env.update(add_env)
                for p in reversed(add_path):
                    if p not in self.extra_path:
                        self.extra_path.insert(0, p)
            else:
                ok, msg = _check_setup_action(step, detail)
            self.say(f"== {name}: {msg}")
            return StepResult(name, "passed" if ok else "failed", msg), {}, 0 if ok else 1
        if actions.action_name(step.uses) == "actions/checkout":
            other = self._checkout_other(step, step_env, scope, name)
            if other is not None:
                return other
        if self.box and getattr(self.box, "artifacts", None) and \
                actions.action_name(step.uses) in ("actions/upload-artifact", "actions/download-artifact"):
            return self._artifact(step, step_env, scope, name)
        if kind in ("noop", "skip"):
            self.say(f"== {name}: skipped ({detail})")
            return StepResult(name, "skipped", f"{step.uses}: {detail}"), {}, 0
        if not self.box:
            self.say(f"== {name}: skipped ({detail}; --docker runs it)")
            return StepResult(name, "skipped", f"{step.uses}: {detail}"), {}, 0
        return self.run_action(step, step_env, scope, name)

    # -- artifacts (Docker mode): kept in a volume the run's jobs share, one folder per workflow

    def _artifact(self, step: Step, step_env: dict[str, Any], scope: _Scope,
                  name: str) -> tuple[StepResult, dict[str, str], int]:
        with_ = self.interp_map(step.with_, step_env, scope)
        upload = actions.action_name(step.uses) == "actions/upload-artifact"
        env = {
            "ART": self.box.artifact_dir,
            "A_NAME": str(with_.get("name") or ("artifact" if upload else "")),
            "A_PATH": str(with_.get("path") or ("" if upload else ".")),
            "A_PATTERN": str(with_.get("pattern") or ""),
            "A_MERGE": str(with_.get("merge-multiple") or "false").lower(),
            "A_IF_NONE": str(with_.get("if-no-files-found") or "warn").lower(),
        }
        out = self.run_script(_UPLOAD_SH if upload else _DOWNLOAD_SH, "bash", self.workspace,
                              {**step_env, **env}, step.timeout_minutes)
        msg = (out.log_text.strip().splitlines() or [""])[-1][:200]
        self.say(f"== {name}: {msg}")
        status = "passed" if out.code == 0 else "failed"
        return StepResult(name, status, msg if out.code else ""), out.outputs, out.code

    # -- real actions (Docker mode)

    def run_action(self, step: Step, step_env: dict[str, Any], scope: _Scope,
                   name: str) -> tuple[StepResult, dict[str, str], int]:
        uses = step.uses.strip()
        if uses.startswith("docker://"):
            return self._skip(name, f"{uses}: docker actions are not run yet"), {}, 0
        if scope.depth >= MAX_ACTION_DEPTH:
            return StepResult(name, "failed", "actions nested too deeply"), {}, 1
        try:
            action = actionfetch.resolve(uses, self.root)
        except (actionfetch.FetchError, OSError) as e:
            return self._skip(name, f"could not get {uses}: {e}"), {}, 0

        ctx = self.ctx(step_env, scope)
        given = {str(k).lower(): v for k, v in step.with_.items()}
        inputs: dict[str, str] = {}
        for key, spec in action.inputs.items():
            spec = spec or {}
            raw = given.get(str(key).lower(), spec.get("default"))
            inputs[str(key)] = expr.interpolate(expr.to_str(raw), ctx) if raw is not None else ""
        for key, raw in step.with_.items():  # inputs the action does not declare still reach it
            if str(key) not in inputs and str(key).lower() not in {k.lower() for k in inputs}:
                inputs[str(key)] = expr.interpolate(expr.to_str(raw), ctx)

        if action.kind == "node":
            return self._run_node(action, step, step_env, inputs, name)
        if action.kind == "composite":
            return self._run_composite(action, step, step_env, inputs, scope, name)
        if action.kind == "docker":
            return self._skip(name, f"{uses}: docker container actions are not run yet"), {}, 0
        return self._skip(name, f"{uses}: runs.using '{action.using}' is not supported"), {}, 0

    def _checkout_other(self, step: Step, step_env: dict[str, Any], scope: _Scope,
                        name: str) -> tuple[StepResult, dict[str, str], int] | None:
        """``actions/checkout`` of another repository: clone it for real (Docker mode).

        A checkout of the repo under test is a no-op (the code is already there);
        returns None for that.
        """
        with_ = self.interp_map(step.with_, step_env, scope)
        repo = (with_.get("repository") or "").strip()
        this = expr.to_str(self.base_values.get("github", {}).get("repository"))
        if not repo or repo.lower() == this.lower():
            return None
        if not self.box:
            return self._skip(name, f"checks out {repo}; --docker clones it"), {}, 0
        dest = (with_.get("path") or "").strip() or "."
        depth = (with_.get("fetch-depth") or "1").strip()
        ref = (with_.get("ref") or "").strip()
        url = f"https://github.com/{repo}.git"
        q = shlex.quote
        if ref and re.fullmatch(r"[0-9a-f]{40}", ref):
            script = (f"git init -q {q(dest)} && cd {q(dest)} && git remote add origin {q(url)} && "
                      f"git fetch -q --depth {q(depth)} origin {q(ref)} && git checkout -q FETCH_HEAD")
        else:
            script = (f"git clone -q {'' if depth == '0' else '--depth ' + q(depth)} "
                      f"{'--branch ' + q(ref) if ref else ''} {q(url)} {q(dest)}")
        self.say(f"== {name}: cloning {repo}{'@' + ref if ref else ''} into {dest}")
        out = self.run_script(script, "bash", self.workspace, {}, step.timeout_minutes)
        if out.code != 0:
            return StepResult(name, "failed", f"could not clone {repo} (exit {out.code})"), {}, out.code
        return StepResult(name, "passed", f"cloned {repo}"), {}, 0

    def _skip(self, name: str, why: str) -> StepResult:
        self.say(f"== {name}: skipped ({why})")
        return StepResult(name, "skipped", why)

    def _action_dir(self, action: actionfetch.Action) -> str:
        """The action's directory inside the container, copying it in on first use."""
        if action.local:
            return posixpath.join(self.workspace, action.path)
        key = action.container_key
        if key not in self.copied:
            repo_root = action.dir
            for _ in range(len(Path(action.path).parts) if action.path else 0):
                repo_root = repo_root.parent
            self.copied[key] = self.box.copy_dir(repo_root, f"{self.box.actions_root}/{key}")
        return posixpath.join(self.copied[key], action.path) if action.path else self.copied[key]

    def _node(self, using: str) -> list[str] | None:
        """PATH entries holding the node runtime an action asks for (node20 → Node 20)."""
        major = re.sub(r"\D", "", using) or "20"
        if int(major) < 20:
            major = "20"  # GitHub runs node12/16 actions on Node 20 too
        if major not in self.node_paths:
            ok, msg, _, paths = self.box.setup_tool("actions/setup-node", {"node-version": major}, "node")
            self.say(f"   node runtime: {msg}")
            self.node_paths[major] = paths if ok else []
            if not ok:
                return None
        return self.node_paths[major] or None

    def _action_env(self, action: actionfetch.Action, step_env: dict[str, Any], inputs: dict[str, str],
                    adir: str) -> tuple[dict[str, Any], dict[str, str]]:
        env = dict(step_env)
        extra = {_input_env(k): v for k, v in inputs.items()}
        extra.update({
            "GITHUB_ACTION_PATH": adir,
            "GITHUB_ACTION_REPOSITORY": action.repo_slug,
            "GITHUB_ACTION_REF": action.ref,
            "GITHUB_ACTION": re.sub(r"\W", "", action.repo or "local"),
        })
        return env, extra

    def _run_node(self, action: actionfetch.Action, step: Step, step_env: dict[str, Any], inputs: dict[str, str],
                  name: str) -> tuple[StepResult, dict[str, str], int]:
        node = self._node(action.using)
        if node is None and self.box.spec:
            if self.box.sh("command -v node >/dev/null", check=False) != 0:
                return self._skip(name, f"{step.uses}: no node in the job container image"), {}, 0
        elif node is None:
            return StepResult(name, "failed", "could not install the node runtime"), {}, 1
        adir = self._action_dir(action)
        env, extra = self._action_env(action, step_env, inputs, adir)
        saved = list(self.extra_path)
        self.extra_path = (node or []) + self.extra_path
        runs = action.runs
        state: dict[str, str] = {}
        try:
            pre = runs.get("pre")
            if pre and self._cond(runs.get("pre-if"), env):
                self.say(f"== {name} (pre)")
                out = self.run_process(["node", posixpath.join(adir, pre)], self.workspace, env, step.timeout_minutes, extra)
                state.update(out.state)
                if out.code != 0:
                    return self._action_failed(name, step, out), out.outputs, out.code
            self.say(f"== {name}: running {action.uses}")
            main = runs.get("main")
            if not main:
                return StepResult(name, "failed", "action has no runs.main"), {}, 1
            extra_main = dict(extra, **{f"STATE_{k}": v for k, v in state.items()})
            out = self.run_process(["node", posixpath.join(adir, main)], self.workspace, env, step.timeout_minutes,
                                   extra_main)
            state.update(out.state)
        finally:
            node_dirs = node or []
            self.extra_path = [p for p in self.extra_path if p not in node_dirs or p in saved]
        post = runs.get("post")
        if post:
            post_if = runs.get("post-if") or "always()"
            post_extra = dict(extra, **{f"STATE_{k}": v for k, v in state.items()})

            def run_post() -> None:
                if not self._cond(post_if, env):
                    return
                self.say(f"== {name} (post)")
                self.extra_path = (node or []) + self.extra_path
                try:
                    res = self.run_process(["node", posixpath.join(adir, post)], self.workspace, env, None, post_extra)
                finally:
                    self.extra_path = [p for p in self.extra_path if p not in (node or [])]
                if res.code != 0:
                    self.say(f"! post step of {name} failed (exit {res.code})")
            self.posts.append(run_post)
        if out.code == 0:
            what = f"local action {action.uses}" if action.local else f"{action.repo_slug}@{action.ref[:12]}"
            return StepResult(name, "passed", f"ran {what}"), out.outputs, 0
        return self._action_failed(name, step, out), out.outputs, out.code

    def _action_failed(self, name: str, step: Step, out: _Outcome) -> StepResult:
        detail = self._failure_detail(out)
        if _NEEDS_GITHUB.search(out.log_text) and not self.token:
            # The code may be fine: the action could not read GitHub without a token.
            detail += (" — the action needs to read GitHub; set PREFLIGHT_GITHUB_TOKEN to a token with no scopes "
                       "(public read only) to let it")
        return StepResult(name, "failed", detail)

    def _cond(self, cond: Any, env: dict[str, Any]) -> bool:
        try:
            return expr.evaluate_condition(cond, self.ctx(env, _Scope(steps={})))
        except expr.ExpressionError:
            return True

    def _run_composite(self, action: actionfetch.Action, step: Step, step_env: dict[str, Any],
                       inputs: dict[str, str], scope: _Scope, name: str) -> tuple[StepResult, dict[str, str], int]:
        adir = self._action_dir(action)
        self.say(f"== {name}: running composite {action.uses}")
        inner = _Scope(steps={}, inputs=inputs, action=action, action_dir=adir, depth=scope.depth + 1,
                       prefix=f"{name} / ")
        raw_steps = action.runs.get("steps") or []
        steps = [step_from_raw(i, s) for i, s in enumerate(raw_steps)]
        for s in steps:
            if s.run is not None and not s.shell:
                s.shell = "bash"  # composite run steps must name a shell; bash is what nearly all use
        env = dict(step_env)
        before = self.status
        results: list[StepResult] = []
        self.run_steps(steps, env, inner, results)
        for r in results:
            self.say(f"   {r.status}: {r.name}{' (' + r.detail + ')' if r.detail else ''}")
        outputs = {}
        ctx = self.ctx(env, inner)
        for key, spec in action.outputs.items():
            value = (spec or {}).get("value") if isinstance(spec, dict) else spec
            try:
                outputs[str(key)] = expr.interpolate(expr.to_str(value), ctx) if value is not None else ""
            except expr.ExpressionError:
                outputs[str(key)] = ""
        failed = next((r for r in results if r.status == "failed"), None)
        if failed:
            self.status = before  # the caller decides (continue-on-error)
            return StepResult(name, "failed", f"'{failed.name}' {failed.detail}"), outputs, 1
        skipped = [r for r in results if r.status == "skipped" and r.detail.endswith("not run yet")]
        note = f"ran composite {action.uses}" + (f"; {len(skipped)} inner steps skipped" if skipped else "")
        return StepResult(name, "passed", note), outputs, 0

    # -- the job

    def run(self) -> JobResult:
        started = time.time()
        label = job_label(self.job, self.combo)
        result = JobResult(workflow=self.wf.name, job_id=self.job.id, label=label, status="passed",
                           log_path=self.log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("w") as log:
            self.log = log
            self.say(f"# {self.wf.name} / {label}" + (f"  [docker: {self.box.image}]" if self.box else ""))
            self.write(self.event_path, json.dumps((self.base_values.get("github") or {}).get("event") or {}))
            for f in self.files.values():
                self.write(f, "")
            scope = _Scope(steps={})
            env: dict[str, Any] = {}
            env.update(self.interp_map(self.wf.env, env, scope))
            env.update(self.interp_map(self.job.env, env, scope))
            self.run_steps(self.job.steps, env, scope, result.steps)
            cancelled = any(s.detail == "cancelled" for s in result.steps)
            if cancelled:
                self.say("! cancelled: another job failed (--keep-going runs everything)")
            elif self.posts:
                for post in reversed(self.posts):
                    post()
        shutil.rmtree(self.host_temp, ignore_errors=True)
        if self.cancel is not None and self.cancel.is_set() and (cancelled or self.status != "success"):
            # A step killed by the cancel is not this job's own failure.
            result.status = "cancelled"
        else:
            result.status = "passed" if self.status == "success" else "failed"
        result.seconds = time.time() - started
        return result


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
    cancel: Any = None,
    echo_prefix: str = "    ",
) -> JobResult:
    """Run one matrix combination of ``job``.

    ``box`` is a started :class:`preflight.docker.DockerJob`; without it the
    steps run directly on this machine in ``root``. When the ``cancel`` event
    is set, the remaining steps are not run and the result is ``cancelled``.
    """
    return _JobRun(wf, job, combo, base_values, root, log_path, runner_os, echo, box, cancel, echo_prefix).run()


def _truthy(value: Any, ctx: expr.Context) -> bool:
    if isinstance(value, str) and "${{" in value:
        return expr.to_bool(expr.interpolate(value, ctx) == "true")
    return expr.to_bool(value)


def _runner_arch() -> str:
    from . import docker as _d
    return _d.arch()[0]


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
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_JOB": "",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_API_URL": "https://api.github.com",
        "GITHUB_GRAPHQL_URL": "https://api.github.com/graphql",
        "RUNNER_OS": runner_os,
        "RUNNER_TEMP": temp,
        "RUNNER_ARCH": _runner_arch(),
        "RUNNER_TOOL_CACHE": "/opt/hostedtoolcache",
        "PREFLIGHT": "1",
    }
