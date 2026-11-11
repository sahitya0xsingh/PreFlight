"""Run jobs inside Docker containers, close to a GitHub-hosted Ubuntu runner.

Each job run gets a fresh container from ``preflight-runner:<ubuntu version>``
(built once from :data:`DOCKERFILE`), or from the job's own ``container:``
image. The workspace is a copy of the checkout, so the job cannot change the
contributor's files, and machine-setup steps (``sudo apt-get install``) run
for real because they only touch the container.

Service containers share the job container's network namespace, so steps
reach them both as ``localhost:<port>`` and by service name, which covers
jobs that run on the runner and jobs that run in a container.

Tool versions asked for by ``setup-java`` / ``setup-node`` / ``setup-python``
/ ``setup-go`` are installed into a shared tool-cache volume, so each version
downloads once.
"""

from __future__ import annotations

import functools
import json
import os
import posixpath
import platform
import re
import shlex
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from . import expr

TOOL_CACHE = "/opt/hostedtoolcache"
CACHE_VOLUMES = {
    "preflight-toolcache": TOOL_CACHE,
    "preflight-m2": "/home/runner/.m2",
    "preflight-gradle": "/home/runner/.gradle",
    "preflight-npm": "/home/runner/.npm",
    "preflight-cache": "/home/runner/.cache",
}

DOCKERFILE = """\
FROM ubuntu:{version}
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \\
      sudo git curl wget ca-certificates gnupg xz-utils unzip zip jq file \\
      build-essential python3 python3-venv python3-pip locales tzdata openssh-client \\
 && (id -u ubuntu >/dev/null 2>&1 && userdel -r ubuntu || true) \\
 && useradd -m -u 1001 -s /bin/bash runner \\
 && echo 'runner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/runner \\
 && mkdir -p {toolcache} /home/runner/.m2 /home/runner/.gradle /home/runner/.npm /home/runner/.cache \\
              /home/runner/work/_temp \\
 && chown -R runner:runner {toolcache} /home/runner \\
 && git config --system --add safe.directory '*'
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
# GitHub's runner image ships Maven; many Java workflows call a bare `mvn`.
RUN curl -fsSL https://archive.apache.org/dist/maven/maven-3/3.9.9/binaries/apache-maven-3.9.9-bin.tar.gz \
      | tar -xz -C /opt && ln -s /opt/apache-maven-3.9.9/bin/mvn /usr/local/bin/mvn
LABEL org.preflight.recipe="{recipe}"
USER runner
ENV RUNNER_TOOL_CACHE={toolcache} AGENT_TOOLSDIRECTORY={toolcache} HOME=/home/runner
WORKDIR /home/runner
"""
RECIPE = "3"  # bump when DOCKERFILE changes so old images are rebuilt


class DockerError(RuntimeError):
    pass


def _docker(*args: str, input: str | None = None, check: bool = True, timeout: float | None = None) -> str:
    proc = subprocess.run(["docker", *args], input=input, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise DockerError(f"docker {args[0]} failed: {(proc.stderr or proc.stdout).strip()[:500]}")
    return proc.stdout


def available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def default_parallel() -> int:
    """Job runs at a time: about one per 2.5 GB of Docker memory (Maven builds are hungry), 1 to 4."""
    try:
        mem = int(_docker("info", "--format", "{{.MemTotal}}", timeout=20).strip() or 0)
        cpus = int(_docker("info", "--format", "{{.NCPU}}", timeout=20).strip() or 1)
    except (DockerError, ValueError, subprocess.TimeoutExpired):
        return 2
    return max(1, min(4, int(mem / 2.5e9), cpus // 2 or 1))


def ubuntu_version(runs_on: Any) -> str:
    text = json.dumps(runs_on).lower()
    m = re.search(r"ubuntu-(\d\d\.\d\d)", text)
    return m.group(1) if m else "24.04"


_IMAGE_LOCK = threading.Lock()
_TAR_LOCK = threading.Lock()


def ensure_runner_image(version: str, say: Callable[[str], None] = print) -> str:
    with _IMAGE_LOCK:  # parallel jobs must not build the same image twice
        return _ensure_runner_image(version, say)


def _ensure_runner_image(version: str, say: Callable[[str], None]) -> str:
    tag = f"preflight-runner:{version}"
    out = _docker("image", "inspect", "--format", '{{ index .Config.Labels "org.preflight.recipe" }}', tag, check=False)
    if out.strip() == RECIPE:
        return tag
    say(f"  building {tag} (once; a few minutes)...")
    dockerfile = DOCKERFILE.format(version=version, toolcache=TOOL_CACHE, recipe=RECIPE)
    _docker("build", "-q", "-t", tag, "-", input=dockerfile, timeout=1800)
    return tag


def arch() -> tuple[str, str]:
    """(runner.arch as GitHub spells it, download name used by most vendors)."""
    m = platform.machine().lower()
    if m in ("arm64", "aarch64"):
        return "ARM64", "arm64"
    return "X64", "x64"


# ------------------------------------------------------------------ setup-* actions


def _fetch_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "preflight-ci"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _version_file(root: Path, name: str | None) -> str | None:
    if not name:
        return None
    f = root / name
    if not f.is_file():
        return None
    text = f.read_text().strip()
    if f.name == "go.mod":
        m = re.search(r"^(?:toolchain go|go )(\d+\.\d+(?:\.\d+)?)", text, re.M)
        return m.group(1) if m else None
    if f.name == "package.json":
        engines = json.loads(text).get("engines", {})
        return engines.get("node")
    return text.splitlines()[0].strip().lstrip("v") if text else None


@functools.lru_cache(maxsize=64)
def resolve_node(spec: str) -> str:
    spec = spec.strip().lstrip("v")
    releases = _fetch_json("https://nodejs.org/dist/index.json")
    if spec in ("latest", "current", "node", "*"):
        return releases[0]["version"].lstrip("v")
    if spec.startswith("lts"):
        codename = spec.split("/", 1)[1] if "/" in spec else "*"
        for r in releases:
            if r["lts"] and (codename == "*" or str(r["lts"]).lower() == codename.lower()):
                return r["version"].lstrip("v")
    prefix = re.sub(r"\.x$|\.\*$", "", spec.lstrip("^~>=").split(" ")[0])
    for r in releases:
        v = r["version"].lstrip("v")
        if v == prefix or v.startswith(prefix + "."):
            return v
    raise DockerError(f"no Node.js release matches {spec!r}")


@functools.lru_cache(maxsize=64)
def resolve_go(spec: str) -> str:
    spec = spec.strip().lstrip("v^~")
    releases = _fetch_json("https://go.dev/dl/?mode=json&include=all")
    stable = [r["version"][2:] for r in releases if r.get("stable")]
    if spec in ("stable", "latest", "*"):
        return stable[0]
    prefix = re.sub(r"\.x$", "", spec)
    for v in stable:
        if v == prefix or v.startswith(prefix + "."):
            return v
    raise DockerError(f"no Go release matches {spec!r}")


def setup_script(action: str, with_: dict[str, Any], root: Path) -> tuple[str, str] | None:
    """Return (shell script, description) that installs what the action asks for.

    The script prints ``KEY=value`` lines for the environment and ``PATH+=dir``
    lines for the PATH; :meth:`DockerJob.setup_tool` reads them.
    """
    gh_arch, dl_arch = arch()
    if action == "actions/setup-java":
        version = str(with_.get("java-version") or _version_file(root, with_.get("java-version-file")) or "")
        if not version:
            return None
        major = re.match(r"\d+", version.split(".")[0] if not version.startswith("1.") else version[2:])
        if not major:
            raise DockerError(f"cannot read java-version {version!r}")
        major = major.group(0)
        dist = str(with_.get("distribution") or "temurin")
        adopt_arch = "aarch64" if dl_arch == "arm64" else "x64"
        d = f"{TOOL_CACHE}/Java_Temurin-Hotspot_jdk/{major}/{gh_arch.lower()}"
        url = f"https://api.adoptium.net/v3/binary/latest/{major}/ga/linux/{adopt_arch}/jdk/hotspot/normal/eclipse"
        script = f"""
if [ ! -x {d}/bin/java ]; then
  mkdir -p {d} && curl -fsSL '{url}' | tar -xz -C {d} --strip-components=1
fi
echo JAVA_HOME={d}
echo JAVA_HOME_{major}_{gh_arch}={d}
echo PATH+={d}/bin
"""
        note = "" if dist.lower() == "temurin" else f" (Temurin used in place of {dist})"
        return script, f"Java {major}{note}"
    if action == "actions/setup-node":
        spec = str(with_.get("node-version") or _version_file(root, with_.get("node-version-file")) or "")
        if not spec:
            return None
        v = resolve_node(spec)
        d = f"{TOOL_CACHE}/node/{v}/{gh_arch.lower()}"
        url = f"https://nodejs.org/dist/v{v}/node-v{v}-linux-{dl_arch}.tar.gz"
        script = f"""
if [ ! -x {d}/bin/node ]; then
  mkdir -p {d} && curl -fsSL '{url}' | tar -xz -C {d} --strip-components=1
fi
echo PATH+={d}/bin
"""
        return script, f"Node.js {v}"
    if action == "actions/setup-python":
        spec = str(with_.get("python-version") or _version_file(root, with_.get("python-version-file")) or "")
        if not spec:
            return None
        req = re.sub(r"\.x$", "", spec.strip())
        if req.startswith("pypy"):
            req = "pypy@" + req[4:]
        script = f"""
export UV_PYTHON_INSTALL_DIR={TOOL_CACHE}/uv-python UV_PYTHON_PREFERENCE=only-managed
uv python install -q '{req}' >&2
p=$(uv python find '{req}')
d=$(dirname "$p")
[ -e "$d/python" ] || ln -s "$p" "$d/python"
rm -f "$d"/../lib/python*/EXTERNALLY-MANAGED
"$p" -m pip --version >/dev/null 2>&1 || "$p" -m ensurepip -q >&2
echo pythonLocation=$(dirname "$d")
echo PATH+=$d
"""
        return script, f"Python {spec}"
    if action == "actions/setup-go":
        spec = str(with_.get("go-version") or _version_file(root, with_.get("go-version-file")) or "")
        if not spec:
            return None
        v = resolve_go(spec)
        d = f"{TOOL_CACHE}/go/{v}/{gh_arch.lower()}"
        url = f"https://go.dev/dl/go{v}.linux-{dl_arch}.tar.gz"
        script = f"""
if [ ! -x {d}/bin/go ]; then
  mkdir -p {d} && curl -fsSL '{url}' | tar -xz -C {d} --strip-components=1
fi
echo GOROOT={d}
echo PATH+={d}/bin
echo PATH+=$HOME/go/bin
"""
        return script, f"Go {v}"
    return None


# ------------------------------------------------------------------ one job run


def _container_spec(job_container: Any, ctx: expr.Context) -> dict[str, Any] | None:
    if not job_container:
        return None
    if isinstance(job_container, str):
        job_container = {"image": job_container}
    spec = json.loads(expr.interpolate(json.dumps(job_container), ctx))
    return spec if spec.get("image") else None


class DockerJob:
    """A running job container plus its service containers."""

    def __init__(self, job, combo: dict[str, Any], ctx: expr.Context, repo: str, root: Path,
                 snapshot: str, say: Callable[[str], None], base_sha: str | None = None):
        self.job = job
        self.root = root
        self.say = say
        self.id = uuid.uuid4().hex[:10]
        self.name = f"preflight-{self.id}"
        self.services: list[str] = []
        self.spec = _container_spec(job.container, ctx)
        repo_name = repo.split("/")[-1]
        self.repo = repo
        self.base_sha = base_sha
        if self.spec:
            self.image = self.spec["image"]
            self.workspace = f"/__w/{repo_name}/{repo_name}"
            self.temp = "/__w/_temp"
            self.actions_root = "/__w/_actions"
        else:
            self.image = ensure_runner_image(ubuntu_version(job.runs_on), say)
            self.workspace = f"/home/runner/work/{repo_name}/{repo_name}"
            self.temp = "/home/runner/work/_temp"
            self.actions_root = "/home/runner/work/_actions"
        self.snapshot = snapshot
        self.ctx = ctx
        self.default_path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        self.has_bash = True
        self.service_ports: dict[str, dict[str, str]] = {}

    # -- lifecycle

    def start(self) -> None:
        args = ["run", "-d", "--name", self.name, "--label", "org.preflight=1"]
        for svc in (self.job.services or {}):
            args += ["--add-host", f"{svc}:127.0.0.1"]
        if self.spec:
            for k, v in (self.spec.get("env") or {}).items():
                args += ["-e", f"{k}={expr.to_str(v)}"]
            args += shlex.split(str(self.spec.get("options") or ""))
            for vol in self.spec.get("volumes") or []:
                args += ["-v", str(vol)]
        else:
            for vol, path in CACHE_VOLUMES.items():
                args += ["-v", f"{vol}:{path}"]
        args += ["--entrypoint", "tail", self.image, "-f", "/dev/null"]
        if self.spec:
            self.say(f"  pulling {self.image} if needed...")
            _docker("pull", "-q", self.image, check=False, timeout=1800)
        _docker(*args, timeout=600)
        self.has_bash = self.sh("command -v bash >/dev/null", check=False) == 0
        path = self.sh_out("echo $PATH").strip()
        if path:
            self.default_path = path
        self.sh(f"mkdir -p {shlex.quote(self.workspace)} {self.temp}/_runner_file_commands", root=True)
        # The runner owns the workspace and its parent (work/<repo>), as on GitHub: some scripts
        # put a second worktree next to the checkout.
        self.sh(f"chown -R $(id -u {self._user()}):$(id -g {self._user()}) {shlex.quote(self.workspace)} {self.temp}"
                f" && chown $(id -u {self._user()}):$(id -g {self._user()}) {shlex.quote(posixpath.dirname(self.workspace))}",
                root=True, check=False)
        self._copy_workspace()
        self._start_services()

    def _user(self) -> str:
        return "root" if self.spec else "runner"

    def stop(self) -> None:
        names = [self.name] + self.services
        _docker("rm", "-f", "-v", *names, check=False, timeout=120)

    def __enter__(self) -> "DockerJob":
        try:
            self.start()
        except Exception:
            self.stop()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- workspace

    def _copy_workspace(self) -> None:
        """Copy the snapshot in and give it a one-commit history, like actions/checkout's default."""
        tar = _snapshot_tar(self.root, self.snapshot)
        proc = subprocess.run(
            ["docker", "exec", "-i", "-u", self._user(), "-w", self.workspace, self.name, "tar", "-xf", "-"],
            input=tar, capture_output=True, timeout=600,
        )
        if proc.returncode != 0:
            raise DockerError(f"copying the workspace failed: {proc.stderr.decode(errors='replace')[:300]}")
        if self.sh("command -v git >/dev/null", check=False) != 0:
            return
        # Like a PR checkout: one commit holding the code, whose parent is the
        # base commit (fetched with depth 1), so `git diff origin/<base>...HEAD`
        # and merge-base work. Without network access the commit has no parent.
        url = f"https://github.com/{self.repo}.git"
        base = shlex.quote(self.base_sha or "")
        script = f"""
git init -q -b preflight .
git remote add origin {shlex.quote(url)}
git add -A
tree=$(git write-tree)
parent=""
if [ -n {base} ] && timeout 120 git fetch -q --depth=1 --no-tags origin {base} 2>/dev/null; then
  parent="-p {base}"
fi
commit=$(git -c user.name=preflight -c user.email=preflight@localhost commit-tree "$tree" $parent -m preflight-snapshot)
git update-ref refs/heads/preflight "$commit"
"""
        self.sh(script, cwd=self.workspace, check=False)

    # -- services

    def _start_services(self) -> None:
        for name, svc in (self.job.services or {}).items():
            svc = json.loads(expr.interpolate(json.dumps(svc or {}), self.ctx))
            image = svc.get("image")
            if not image:
                continue
            cname = f"{self.name}-{name}"
            args = ["run", "-d", "--name", cname, "--label", "org.preflight=1", "--network", f"container:{self.name}"]
            for k, v in (svc.get("env") or {}).items():
                args += ["-e", f"{k}={expr.to_str(v)}"]
            args += _service_options(str(svc.get("options") or ""))
            for vol in svc.get("volumes") or []:
                args += ["-v", str(vol)]
            args.append(image)
            self.say(f"  starting service {name} ({image})...")
            _docker("pull", "-q", image, check=False, timeout=1800)
            _docker(*args, timeout=600)
            self.services.append(cname)
            ports = {}
            for p in svc.get("ports") or []:
                parts = str(p).split(":")
                ports[parts[-1].split("/")[0]] = parts[-1].split("/")[0]
            self.service_ports[name] = ports
        for cname in self.services:
            self._wait_healthy(cname)

    def _wait_healthy(self, cname: str, limit: float = 180) -> None:
        deadline = time.time() + limit
        while time.time() < deadline:
            state = _docker("inspect", "--format", "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
                            cname, check=False).split()
            if not state or state[0] in ("exited", "dead"):
                logs = _docker("logs", "--tail", "20", cname, check=False)
                raise DockerError(f"service {cname} stopped:\n{logs}")
            if len(state) == 1 or state[1] == "healthy":
                return
            time.sleep(2)
        raise DockerError(f"service {cname} did not become healthy in {limit:.0f}s")

    def services_context(self) -> dict[str, Any]:
        return {name: {"id": f"{self.name}-{name}", "network": self.name, "ports": ports}
                for name, ports in self.service_ports.items()}

    # -- running commands

    def sh(self, script: str, cwd: str | None = None, root: bool = False, check: bool = True) -> int:
        argv = ["exec", "-u", "root" if root else self._user()]
        if cwd:
            argv += ["-w", cwd]
        proc = subprocess.run(["docker", *argv, self.name, "sh", "-c", script], capture_output=True, text=True,
                              timeout=600)
        if check and proc.returncode != 0:
            raise DockerError(f"`{script[:80]}` failed in the container: {(proc.stderr or proc.stdout)[:300]}")
        return proc.returncode

    def oom_kills(self) -> int:
        """Processes in this container the kernel killed for want of memory."""
        out = self.sh_out("cat /sys/fs/cgroup/memory.events 2>/dev/null")
        m = re.search(r"^oom_kill (\d+)", out, re.M)
        return int(m.group(1)) if m else 0

    def sh_out(self, script: str) -> str:
        return _docker("exec", "-u", self._user(), self.name, "sh", "-c", script, check=False, timeout=120)

    def write_file(self, path: str, content: str) -> None:
        # Content goes in argv, not stdin: `docker exec -i` with a short or empty
        # stdin sometimes never returns.
        subprocess.run(["docker", "exec", "-u", self._user(), self.name, "sh", "-c", 'printf %s "$1" > "$2"',
                        "sh", content, path], check=True, capture_output=True, timeout=60)

    def copy_dir(self, host_dir: Path, dest: str) -> str:
        """Copy a host directory (an action's files) to ``dest`` in the container."""
        flags = ["--no-mac-metadata"] if platform.system() == "Darwin" else []
        tar = subprocess.run(["tar", "-cf", "-", *flags, "--exclude=.preflight-fetched", "-C", str(host_dir), "."],
                             capture_output=True, check=True, env={**os.environ, "COPYFILE_DISABLE": "1"}).stdout
        self.sh(f"mkdir -p {shlex.quote(dest)}")
        proc = subprocess.run(["docker", "exec", "-i", "-u", self._user(), "-w", dest, self.name, "tar", "-xf", "-"],
                              input=tar, capture_output=True, timeout=300)
        if proc.returncode != 0:
            raise DockerError(f"copying {host_dir.name} into the container failed: "
                              f"{proc.stderr.decode(errors='replace')[:300]}")
        return dest

    def read_and_clear(self, path: str) -> str:
        q = shlex.quote(path)
        return self.sh_out(f"if [ -f {q} ]; then cat {q}; : > {q}; fi")

    def popen(self, argv: list[str], cwd: str, env: dict[str, str]) -> subprocess.Popen:
        args = ["docker", "exec", "-u", self._user(), "-w", cwd]
        for k, v in env.items():
            args += ["-e", f"{k}={v}"]
        return subprocess.Popen(args + [self.name, *argv], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def setup_tool(self, action: str, with_: dict[str, Any], tool: str) -> tuple[bool, str, dict[str, str], list[str]]:
        """Install what a setup-* action asks for. Returns (ok, message, env, path entries)."""
        try:
            made = setup_script(action, with_, self.root) if not self.spec else None
        except (DockerError, OSError, ValueError) as e:
            return False, str(e), {}, []
        if made is None:
            ok = self.sh(f"command -v {shlex.quote(tool)} >/dev/null", check=False) == 0
            return ok, (f"{tool} found in the image" if ok else f"{tool} is not in the image"), {}, []
        script, what = made
        # Containers share the tool-cache volume: one install at a time, so two
        # jobs never unpack the same version into the same directory.
        script = f"exec 9>{TOOL_CACHE}/.setup.lock\nflock 9\n" + script
        proc = subprocess.run(["docker", "exec", "-u", self._user(), self.name, "bash", "-eo", "pipefail", "-c", script],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            return False, f"installing {what} failed: {proc.stderr.strip()[-300:]}", {}, []
        env, paths = {}, []
        for line in proc.stdout.splitlines():
            if line.startswith("PATH+="):
                paths.append(line[6:])
            elif "=" in line:
                k, v = line.split("=", 1)
                env[k] = v
        return True, f"installed {what}", env, paths


def _service_options(options: str) -> list[str]:
    """Docker flags from a service's ``options:``, minus ones that clash with a shared network."""
    out, skip_next = [], False
    banned = {"--network", "--net", "-p", "--publish", "--hostname", "-h", "--add-host", "--dns", "--name"}
    for tok in shlex.split(options):
        if skip_next:
            skip_next = False
            continue
        flag = tok.split("=", 1)[0]
        if flag in banned:
            skip_next = "=" not in tok
            continue
        out.append(tok)
    return out


_TAR_CACHE: dict[tuple[str, str], bytes] = {}


def _snapshot_tar(root: Path, snapshot: str) -> bytes:
    """A tar of the code to test: ``HEAD`` (commits only) or ``worktree`` (also uncommitted files)."""
    with _TAR_LOCK:
        return _snapshot_tar_locked(root, snapshot)


def _snapshot_tar_locked(root: Path, snapshot: str) -> bytes:
    key = (str(root), snapshot)
    if key in _TAR_CACHE:
        return _TAR_CACHE[key]
    if snapshot == "head":
        data = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", "HEAD"],
                              capture_output=True, check=True).stdout
    else:
        files = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                               capture_output=True, check=True).stdout
        existing = b"\0".join(f for f in files.split(b"\0") if f and (root / f.decode()).exists() or
                              (f and (root / f.decode()).is_symlink()))
        flags = ["--no-mac-metadata"] if platform.system() == "Darwin" else []
        data = subprocess.run(["tar", "-cf", "-", *flags, "--null", "-T", "-"], cwd=root, input=existing,
                              capture_output=True, check=True, env={**os.environ, "COPYFILE_DISABLE": "1"}).stdout
    _TAR_CACHE[key] = data
    return data


def clear_snapshot_cache() -> None:
    _TAR_CACHE.clear()


__all__ = ["DockerJob", "DockerError", "available", "ensure_runner_image", "clear_snapshot_cache"]
