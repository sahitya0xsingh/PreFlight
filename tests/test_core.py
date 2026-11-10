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
