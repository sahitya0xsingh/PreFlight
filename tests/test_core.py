import subprocess
import textwrap
from pathlib import Path

import pytest

from preflight import check, expr, globs, hook, workflow
from preflight.runner import run_job

# ------------------------------------------------------------------ expressions


def ctx(**values):
    return expr.Context(values=values)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("github.event.action != 'closed'", True),
        ("github.repository == 'checkstyle/checkstyle'", True),
        ("github.repository == 'CheckStyle/CheckStyle'", True),  # case-insensitive
        ("github.event.action != 'closed' && github.repository == 'other/repo'", False),
        ("!github.event.pull_request.draft", True),
        ("contains(github.event.pull_request.labels.*.name, 'skip')", False),
        ("startsWith(github.head_ref, 'fix')", True),
        ("matrix.java >= 17", True),
        ("matrix['java'] == '21'", True),
        ("null || 'fallback'", "fallback"),
        ("format('{0}-{1}', 'a', 2)", "a-2"),
    ],
)
def test_evaluate(text, expected):
    c = ctx(
        github={
            "repository": "checkstyle/checkstyle",
            "head_ref": "fix-npe",
            "event": {"action": "synchronize", "pull_request": {"draft": False, "labels": [{"name": "bug"}]}},
        },
        matrix={"java": 21},
    )
    assert expr.evaluate(text, c) == expected


def test_condition_implies_success():
    c = expr.Context(values={}, status="failure")
    assert expr.evaluate_condition(None, c) is False
    assert expr.evaluate_condition("true", c) is False
    assert expr.evaluate_condition("always()", c) is True
    assert expr.evaluate_condition("${{ failure() }}", c) is True


def test_interpolate_records_secret_reads():
    c = ctx(secrets={})
    assert expr.interpolate("token=${{ secrets.NPM_TOKEN }}!", c) == "token=!"
    assert "secrets.NPM_TOKEN" in c.reads


# ------------------------------------------------------------------ path filters


def test_path_filters():
    assert globs.triggers(["src/main/A.java"], ["src/**"], None)
    assert not globs.triggers(["docs/x.md"], ["src/**"], None)
    assert globs.triggers(["README.md"], ["**.md"], None)
    assert globs.triggers(["a/b/c.md"], ["**/*.md"], None)
    assert not globs.triggers(["docs/x.md"], None, ["docs/**"])
    assert globs.triggers(["docs/x.md", "src/A.java"], None, ["docs/**"])
    # a later negation excludes an earlier include
    assert not globs.triggers(["src/gen/X.java"], ["src/**", "!src/gen/**"], None)


# ------------------------------------------------------------------ workflows

WF = textwrap.dedent(
    """
    name: CI
    on:
      pull_request:
        paths-ignore: ['docs/**']
    jobs:
      test:
        if: github.event.action != 'closed'
        runs-on: ubuntu-latest
        strategy:
          matrix:
            java: [17, 21]
            os: [a, b]
            exclude:
              - java: 17
                os: b
            include:
              - java: 21
                extra: yes
        steps:
          - uses: actions/checkout@v4
          - run: echo hi
      windows:
        runs-on: windows-latest
        steps:
          - run: echo hi
      deploy:
        runs-on: ubuntu-latest
        steps:
          - run: ./deploy --token "${{ secrets.DEPLOY_TOKEN }}"
      label:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/labeler@v5
      db:
        runs-on: ubuntu-latest
        services:
          postgres: {image: postgres}
        steps:
          - run: echo hi
      odd:
        runs-on: ubuntu-latest
        steps:
          - uses: someone/thing@v1
          - run: echo hi
    """
)


def test_parse_and_classify():
    wf = workflow.parse(WF, "ci.yml")
    assert wf.pr_event == "pull_request"
    assert wf.pr_filters() == (None, ["docs/**"])
    verdicts = {}
    for job in wf.jobs:
        workflow.classify(job, "Linux")
        verdicts[job.id] = job.verdict
    assert verdicts == {
        "test": "run",
        "windows": "other-os",
        "deploy": "secrets",
        "label": "no-code",
        "db": "docker",
        "odd": "partial",
    }


def test_expand_matrix():
    wf = workflow.parse(WF, "ci.yml")
    combos = workflow.expand_matrix(wf.jobs[0].matrix)
    assert {"java": 17, "os": "b"} not in combos
    assert {"java": 21, "os": "a", "extra": True} in combos
    assert {"java": 21, "os": "b", "extra": True} in combos
    assert len(combos) == 3


# ------------------------------------------------------------------ runner + check on a real repo


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("PREFLIGHT_HOME", str(tmp_path / "home"))
    root = tmp_path / "repo"
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text(textwrap.dedent(
        """
        name: CI
        on: [pull_request]
        jobs:
          lint:
            runs-on: ubuntu-latest
            steps:
              - name: Install deps
                run: sudo apt-get install -y shellcheck
              - id: pick
                run: |
                  echo "GREETING=hello" >> "$GITHUB_ENV"
                  echo "value=42" >> "$GITHUB_OUTPUT"
              - name: use values
                run: |
                  test "$GREETING" = hello
                  test "${{ steps.pick.outputs.value }}" = 42
                  test "$GITHUB_EVENT_NAME" = pull_request
              - name: must pass
                run: test -f ok.txt
              - name: on failure
                if: failure()
                run: echo "cleanup ran" > cleanup.txt
        """
    ))
    (root / ".github" / "workflows" / "docs.yml").write_text(textwrap.dedent(
        """
        name: Docs
        on:
          pull_request:
            paths: ['docs/**']
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: echo docs
        """
    ))
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "remote", "add", "origin", "https://github.com/acme/widget.git")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    _git(root, "checkout", "-qb", "feature")
    (root / "app.py").write_text("print(1)\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "change")
    return root


def _plan(root):
    return check.build_plan(root, "acme/widget", "main", ["app.py"], None, None, quick=False)


def test_plan_respects_path_filters(repo):
    plan = _plan(repo)
    assert [p.job.id for p in plan.to_run] == ["lint"]
    assert plan.not_triggered == [("Docs", "your change does not touch its paths")]


def test_run_job_fails_then_passes(repo, tmp_path):
    plan = _plan(repo)
    p = plan.to_run[0]
    values = check.github_context(repo, "acme/widget", "main", "pull_request")

    res = run_job(p.wf, p.job, {}, values, repo, tmp_path / "1.log", "Linux")
    assert res.status == "failed"
    assert res.failed_step.name == "must pass"
    statuses = [s.status for s in res.steps]
    assert statuses == ["skipped", "passed", "passed", "failed", "passed"]
    assert (repo / "cleanup.txt").exists()  # if: failure() step ran

    (repo / "ok.txt").write_text("")
    res = run_job(p.wf, p.job, {}, values, repo, tmp_path / "2.log", "Linux")
    assert res.status == "passed"


def test_hook_install_and_refuse_foreign(repo):
    path = hook.install(repo, ["--quick"], force=False)
    assert "preflight check --hook --quick" in path.read_text()
    assert hook.uninstall(repo)
    path.write_text("#!/bin/sh\necho mine\n")
    with pytest.raises(SystemExit):
        hook.install(repo, [], force=False)


def test_pr_metadata_job_is_left_to_github():
    wf = workflow.parse(textwrap.dedent(
        """
        on: pull_request
        jobs:
          desc:
            runs-on: ubuntu-latest
            steps:
              - run: ./check.sh
                env:
                  PULL_REQUEST: '${{ github.event.number }}'
          build:
            runs-on: ubuntu-latest
            steps:
              - run: echo "${{ github.event.pull_request.head.ref }}"
        """
    ), "x.yml")
    for j in wf.jobs:
        workflow.classify(j, "Linux")
    assert [j.verdict for j in wf.jobs] == ["pr-metadata", "run"]


# ------------------------------------------------------------------ docker mode

from preflight import docker as dockermod


def test_docker_mode_runs_service_jobs():
    wf = workflow.parse(WF, "ci.yml")
    verdicts = {}
    for job in wf.jobs:
        workflow.classify(job, "macOS", docker=True)
        verdicts[job.id] = (job.verdict, job.reasons)
    assert verdicts["db"][0] == "run"
    assert verdicts["windows"][0] == "other-os"
    assert verdicts["test"] == ("run", [])  # no "Linux job run on macOS" note in a container


def test_docker_helpers():
    assert dockermod.ubuntu_version("ubuntu-22.04") == "22.04"
    assert dockermod.ubuntu_version(["self-hosted", "ubuntu-20.04"]) == "20.04"
    assert dockermod.ubuntu_version("ubuntu-latest") == "24.04"
    opts = "--health-cmd 'pg_isready -U x' --network foo -p 5432:5432 --health-retries=5 --hostname=db"
    assert dockermod._service_options(opts) == ["--health-cmd", "pg_isready -U x", "--health-retries=5"]
    script, what = dockermod.setup_script("actions/setup-java", {"java-version": "21", "distribution": "zulu"}, Path("."))
    assert what == "Java 21 (Temurin used in place of zulu)"
    assert "JAVA_HOME_21_" in script and "api.adoptium.net/v3/binary/latest/21/" in script
    assert dockermod.setup_script("actions/setup-java", {}, Path(".")) is None


def _docker_ready():
    return dockermod.available() and subprocess.run(
        ["docker", "image", "inspect", "preflight-runner:24.04"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _docker_ready(), reason="needs Docker and the preflight-runner:24.04 image")
def test_docker_job_is_isolated(repo, tmp_path):
    plan = check.build_plan(repo, "acme/widget", "main", ["app.py"], None, None, quick=False, docker=True)
    p = plan.to_run[0]
    values = check.github_context(repo, "acme/widget", "main", "pull_request")
    ctx = expr.Context(values=dict(values, matrix={}))
    with dockermod.DockerJob(p.job, {}, ctx, "acme/widget", repo, "worktree", say=lambda m: None) as box:
        res = run_job(p.wf, p.job, {}, values, repo, tmp_path / "d.log", "Linux", box=box)
    # The apt step really runs in the container; ok.txt is missing, so the job fails...
    assert [s.status for s in res.steps][:2] == ["passed", "passed"]
    assert res.failed_step.name == "must pass"
    # ...and nothing the job wrote reached the checkout.
    assert not (repo / "cleanup.txt").exists()
    dockermod.clear_snapshot_cache()


@pytest.mark.skipif(not _docker_ready(), reason="needs Docker and the preflight-runner:24.04 image")
def test_parallel_needs_and_fail_fast(tmp_path, monkeypatch):
    monkeypatch.setenv("PREFLIGHT_HOME", str(tmp_path / "home"))
    root = tmp_path / "par"
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text(textwrap.dedent(
        """
        on: [pull_request]
        jobs:
          bad:
            runs-on: ubuntu-latest
            steps: [{run: "sleep 1 && exit 1"}]
          slow:
            runs-on: ubuntu-latest
            steps: [{run: "sleep 60"}]
          after:
            needs: bad
            runs-on: ubuntu-latest
            steps: [{run: "true"}]
        """
    ))
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    plan = check.build_plan(root, "acme/par", "main", ["x"], None, None, quick=False, docker=True)
    t0 = __import__("time").time()
    results = check.execute(plan, root, fail_fast=True, verbose=False, jobs=3)
    assert {r.job_id: r.status for r in results} == {"bad": "failed", "slow": "cancelled"}
    assert __import__("time").time() - t0 < 40  # slow was stopped, not waited for


# ------------------------------------------------------------------ real actions

from preflight import actionfetch


def test_parse_uses():
    assert actionfetch.parse_uses("actions/cache/restore@v4") == ("actions", "cache", "restore", "v4")
    assert actionfetch.parse_uses("o/r@0123") == ("o", "r", "", "0123")
    with pytest.raises(actionfetch.FetchError):
        actionfetch.parse_uses("o/r")


def test_native_mode_points_unknown_actions_at_docker():
    wf = workflow.parse(WF, "ci.yml")
    odd = next(j for j in wf.jobs if j.id == "odd")
    workflow.classify(odd, "Linux")
    assert odd.verdict == "partial" and "--docker runs it" in odd.reasons[0]


@pytest.mark.skipif(not _docker_ready(), reason="needs Docker and the preflight-runner:24.04 image")
def test_local_composite_and_node_actions(tmp_path, monkeypatch):
    monkeypatch.setenv("PREFLIGHT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PREFLIGHT_GITHUB_TOKEN", "dummy-token")
    root = tmp_path / "act"
    files = {
        ".github/actions/greet/action.yml": """
            inputs: {who: {default: world}}
            outputs: {message: {value: "${{ steps.m.outputs.msg }}"}}
            runs:
              using: composite
              steps:
                - id: m
                  shell: bash
                  run: echo "msg=hello ${{ inputs.who }}" >> "$GITHUB_OUTPUT"
                - uses: ./.github/actions/js
                  with: {tag: inner}
            """,
        ".github/actions/js/action.yml": "runs: {using: node20, pre: pre.js, main: main.js}\n",
        ".github/actions/js/pre.js": "require('fs').appendFileSync(process.env.GITHUB_STATE, 'p=' + process.env.INPUT_TAG + '\\n')\n",
        ".github/actions/js/main.js": textwrap.dedent("""
            if (process.env.INPUT_TAG === 'boom') { console.log('::error::no boom'); process.exit(3) }
            if (process.env.STATE_p !== process.env.INPUT_TAG) process.exit(4)
            console.log('::set-output name=seen::' + process.env.INPUT_TAG)
            """),
        ".github/workflows/ci.yml": """
            on: [pull_request]
            jobs:
              ok:
                runs-on: ubuntu-latest
                steps:
                  - {id: g, uses: ./.github/actions/greet, with: {who: dev}}
                  - {id: j, uses: ./.github/actions/js, with: {tag: top}}
                  - run: test "${{ steps.g.outputs.message }}/${{ steps.j.outputs.seen }}" = "hello dev/top"
                  - run: test "${{ github.token }}/${{ secrets.GITHUB_TOKEN }}" = "dummy-token/dummy-token"
              bad:
                runs-on: ubuntu-latest
                steps:
                  - {uses: ./.github/actions/js, with: {tag: boom}}
            """,
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(textwrap.dedent(text))
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    plan = check.build_plan(root, "acme/act", "main", ["x"], None, None, quick=False, docker=True)
    assert all(p.job.verdict == "run" for p in plan.to_run)
    results = {r.job_id: r for r in check.execute(plan, root, fail_fast=False, verbose=False, jobs=2)}
    assert results["ok"].status == "passed", results["ok"].steps
    assert results["bad"].status == "failed"
    assert "no boom" in results["bad"].failed_step.detail


from preflight import bench


def _matrix_job():
    wf = workflow.parse(textwrap.dedent("""
        on: pull_request
        jobs:
          test:
            strategy:
              matrix:
                target: [java, javadoc]
                include:
                  - target: java
                    CONFIG: a.xml
                  - target: extra
                    CONFIG: b.xml
            runs-on: ubuntu-latest
            steps: [{run: "true"}]
          named:
            name: Build ${{ matrix.v }}
            strategy: {matrix: {v: [1]}}
            runs-on: ubuntu-latest
            steps: [{run: "true"}]
    """), "ci.yml")
    return wf.jobs


def test_bench_github_job_names():
    test, named = _matrix_job()
    names = [bench.github_name(test, c) for c in workflow.expand_matrix(test.matrix)]
    # include-added keys are not part of the name; an include-only combination shows its values
    assert names == ["test (java)", "test (javadoc)", "test (extra)"]
    assert bench.github_name(named, {"v": 1}) == "Build 1"
    gh = [bench.GhJob("ci.yml", "test (javadoc)", "success", 1, ""),
          bench.GhJob("other.yml", "test (java)", "failure", 1, "")]
    assert bench.match(gh, "ci.yml", test, {"target": "javadoc"}) is gh[0]
    assert bench.match(gh, "ci.yml", test, {"target": "java", "CONFIG": "a.xml"}) is None


def test_bench_report(tmp_path, monkeypatch):
    monkeypatch.setenv("PREFLIGHT_HOME", str(tmp_path))
    d = bench.bench_dir("o/r")
    d.mkdir(parents=True)
    rows = [bench.Row("s1", "t", "ci.yml", "a", "success", "passed"),
            bench.Row("s1", "t", "ci.yml", "b", "failure", "failed"),
            bench.Row("s1", "t", "ci.yml", "c", "failure", "passed", "missed"),
            bench.Row("s1", "t", "ci.yml", "d", "success", "left-to-github", "other-os: needs Windows"),
            bench.Row("s1", "t", "ci.yml", "e", "success", "not-sampled", "not in this case's sample")]
    import json as _json
    from dataclasses import asdict
    (d / "results.jsonl").write_text("".join(_json.dumps(asdict(r)) + "\n" for r in rows))
    text = bench.report("o/r")
    assert "Agreement: 66.7% (2/3)" in text
    assert "| Missed failures (local pass, GitHub fail) | 1 |" in text
    assert "Coverage: GitHub jobs PreFlight can run | 80.0% (4/5)" in text
    assert "ci.yml / c" in text


@pytest.mark.skipif(not _docker_ready(), reason="needs Docker and the preflight-runner:24.04 image")
def test_checkout_of_another_repo_clones_it(tmp_path, monkeypatch):
    monkeypatch.setenv("PREFLIGHT_HOME", str(tmp_path / "home"))
    root = tmp_path / "r"
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text(textwrap.dedent("""
        on: pull_request
        jobs:
          j:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
              - uses: actions/checkout@v4
                with: {repository: octocat/Hello-World, path: ./.ci-temp/hello}
              - run: test -f .ci-temp/hello/README && test -f app.txt
    """))
    (root / "app.txt").write_text("x")
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"], check=True)
    plan = check.build_plan(root, "acme/widget", "main", ["app.txt"], None, None, quick=False, docker=True)
    p = plan.to_run[0]
    values = check.github_context(root, "acme/widget", "main", "pull_request")
    ctx = expr.Context(values=dict(values, matrix={}))
    with dockermod.DockerJob(p.job, {}, ctx, "acme/widget", root, "worktree", say=lambda m: None) as box:
        res = run_job(p.wf, p.job, {}, values, root, tmp_path / "c.log", "Linux", box=box)
    assert [s.status for s in res.steps] == ["skipped", "passed", "passed"], (tmp_path / "c.log").read_text()
    dockermod.clear_snapshot_cache()


@pytest.mark.skipif(not _docker_ready(), reason="needs Docker and the preflight-runner:24.04 image")
def test_oom_kill_is_detected_and_workspace_parent_is_writable(repo, tmp_path):
    job = workflow.parse(textwrap.dedent("""
        on: pull_request
        jobs:
          j:
            runs-on: ubuntu-latest
            steps:
              - run: mkdir ../sibling && echo ok
    """), "x.yml").jobs[0]
    values = check.github_context(repo, "acme/widget", "main", "pull_request")
    ctx = expr.Context(values=dict(values, matrix={}))
    with dockermod.DockerJob(job, {}, ctx, "acme/widget", repo, "worktree", say=lambda m: None) as box:
        wf = workflow.parse("on: pull_request\njobs: {}", "x.yml")
        res = run_job(wf, job, {}, values, repo, tmp_path / "o.log", "Linux", box=box)
        assert res.status == "passed", (tmp_path / "o.log").read_text()
        assert box.oom_kills() == 0
    dockermod.clear_snapshot_cache()


def test_timing_jobs_are_left_to_github():
    job = workflow.parse(textwrap.dedent("""
        on: pull_request
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - name: Run performance test
                run: ./perf.sh
    """), "p.yml").jobs[0]
    workflow.classify(job, "Linux", docker=True)
    assert job.verdict == workflow.TIMING


def test_job_using_outputs_of_a_skipped_step_is_incomplete():
    job = workflow.parse(textwrap.dedent("""
        on: pull_request
        jobs:
          j:
            runs-on: ubuntu-latest
            steps:
              - id: links
                uses: some/unknown-action@v1
              - run: echo ${{ steps.links.outputs.issues }}
    """), "p.yml").jobs[0]
    workflow.classify(job, "Linux", docker=False)
    assert job.verdict == workflow.INCOMPLETE


def test_glob_question_mark_is_optional_previous_char():
    from preflight import globs
    assert globs.match("**/*.yaml?", ".github/workflows/tests.yaml")
    assert globs.match("**/*.yaml?", "a/b.yam")
    assert not globs.match("**/*.yaml?", "a/b.yml")


def test_runs_on_from_matrix_splits_per_runner(tmp_path):
    from preflight import check, workflow
    wf = workflow.parse("""
on: pull_request
jobs:
  tests:
    name: ${{ matrix.name || matrix.python }}
    runs-on: ${{ matrix.os || 'ubuntu-latest' }}
    strategy:
      matrix:
        include:
          - {python: '3.13'}
          - {name: Windows, python: '3.13', os: windows-latest}
    steps:
      - run: pytest
""", "t.yml")
    job = wf.jobs[0]
    groups = check._runner_groups(job, workflow.expand_matrix(job.matrix), {})
    verdicts = {}
    for j, cs in groups:
        workflow.classify(j, "Linux", True, tmp_path)
        verdicts[j.runs_on] = (j.verdict, len(cs))
    assert verdicts == {"ubuntu-latest": ("run", 1), "windows-latest": ("other-os", 1)}


def test_python_version_from_pyproject(tmp_path):
    from preflight import docker
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nrequires-python = ">=3.11"\n')
    assert docker._version_file(tmp_path, "pyproject.toml") == ">=3.11"


def test_artifacts_pass_between_docker_jobs(tmp_path):
    import shutil, subprocess
    import pytest
    from preflight import check, docker, gitinfo
    if not shutil.which("docker") or not docker.available():
        pytest.skip("needs Docker")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("""
on: pull_request
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        n: [1, 2]
    steps:
      - run: mkdir -p coverage && echo "SF:${{ matrix.n }}" > coverage/lcov.info
      - uses: actions/upload-artifact@v4
        with:
          name: coverage-${{ matrix.n }}
          path: ./coverage/lcov.info
  merge:
    needs: test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/download-artifact@v4
        with:
          path: ./coverage
          pattern: coverage-*
      - run: test "$(cat coverage/coverage-1/lcov.info coverage/coverage-2/lcov.info)" = "$(printf 'SF:1\\nSF:2')"
""")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"], cwd=tmp_path, check=True)
    plan = check.build_plan(tmp_path, "o/r", "main", [".github/workflows/ci.yml"], None, None, quick=False, docker=True)
    results = check.execute(plan, tmp_path, fail_fast=True, verbose=False, snapshot="head", jobs=2)
    assert [r.status for r in results] == ["passed", "passed", "passed"], [(r.label, r.status, r.steps) for r in results]
