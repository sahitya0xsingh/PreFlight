<p align="center"><img src="assets/logo.svg" width="96" alt="PreFlight logo: a commit dot whose check mark lifts off as an arrow"></p>

# PreFlight

Learn a GitHub org's CI once, run it on your machine, and push only when it passes.
Maintainers get green PRs, and the org spends CI minutes once per PR instead of on every red retry.

```bash
pip install -e .                       # from this folder
preflight learn checkstyle             # scan the org, write ~/.preflight/profiles/checkstyle.json
cd ~/checkstyle
preflight plan                         # which CI jobs your change triggers, and which can run here
preflight check                        # run them; stops at the first failure and prints the log
preflight install-hook                 # from now on, `git push` runs `preflight check` first
```

Skip the hook once with `PREFLIGHT_SKIP=1 git push` or `git push --no-verify`.

## Results

**[Live dashboard →](https://sahitya0xsingh.github.io/preflight/)** · **[Step-by-step guide →](https://sahitya0xsingh.github.io/preflight/#guide)**

`preflight bench` replays real pull requests: it rebuilds the merge commit GitHub tested, runs the jobs in
Docker, and compares each job's pass/fail with GitHub's.

| Project | Language | PRs | Job runs compared | Agreement | Failures caught | False alarms |
| --- | --- | --- | --- | --- | --- | --- |
| [checkstyle/checkstyle](https://github.com/checkstyle/checkstyle) | Java (Maven) | 25 | 78 | **98.7%** | 25/26 | 0 |
| [pallets/flask](https://github.com/pallets/flask) | Python (uv, tox) | 20 | 117 | **100%** | 13/13 | 0 |
| [expressjs/express](https://github.com/expressjs/express) | JavaScript (Node.js, npm) | 20 | 114 | **100%** | 33/33 | 0 |
| **All three** | | **65** | **309** | **99.7%** | **71/72** | **0** |

Coverage (the share of GitHub's jobs PreFlight can run on a Mac) is 88% for checkstyle and Flask and 55% for
Express, whose test matrix runs every Node.js version on Windows too; Windows and macOS jobs stay on GitHub.

The dashboard lists every compared job run with a link to GitHub's own job page. Rebuild its data with
`npm --prefix dashboard run data` after a benchmark.

## Docker mode

```bash
preflight check --docker               # each job in a fresh Ubuntu container
preflight install-hook --docker        # the same, before every push
```

- Every job run starts from `preflight-runner:<ubuntu version>` (built once, picked from `runs-on`), or from
  the job's own `container:` image. The code is copied in, so jobs never change your checkout. In hook mode only
  committed code is copied.
- Machine-setup steps (`sudo apt-get install …`) run for real, because they only touch the container.
- `setup-java`, `setup-node`, `setup-python` and `setup-go` install the version the workflow (or matrix entry)
  asks for. Versions are kept in the `preflight-toolcache` volume, and Maven/Gradle/npm caches in their own
  volumes, so each downloads once.
- `services:` containers start next to the job and are waited on until healthy. Steps reach them as
  `localhost:<port>` and by service name.
- Marketplace actions PreFlight does not emulate are run for real: JavaScript actions (with `pre`/`post` steps,
  inputs, outputs and saved state, on the Node version the action declares), composite actions, and local
  `uses: ./path` actions, nested to any depth. Each is downloaded once at its pinned ref into `~/.preflight/actions`.
  Docker container actions (`runs.using: docker`) are skipped for now, and actions that start Docker containers
  themselves (Qodana, `docker/build-push-action`, …) leave their job to GitHub.
- Jobs get no GitHub token unless you give one: `PREFLIGHT_GITHUB_TOKEN=<token> preflight check --docker` passes it
  as `github.token` and `secrets.GITHUB_TOKEN`. Third-party action code runs with it, so use a classic token with
  **no scopes** (it can read public data, nothing else). PreFlight never reads your `gh` login for this.
- Jobs run in parallel: by default about one per 2.5 GB of Docker memory, up to 4 (`--jobs N` / `-j N` to change).
  A job starts once every job it `needs` has passed. The first failure stops the running containers and cancels the
  rest, unless you pass `--keep-going`; with it, jobs that need a failed job are reported as not run.
- `upload-artifact` / `download-artifact` pass files between the jobs of a run (through a Docker volume kept for
  that run, one folder per workflow), so a `coverage` job that merges its test jobs' reports works as on GitHub.
- A matrix whose `runs-on` reads the matrix (`${{ matrix.os }}`) is split per runner: the Linux entries run here.
- Jobs that need Windows or macOS runners, secrets, or the PR itself are still left to GitHub. On an ARM Mac, a
  tool version with no ARM64 build (Node.js 0.12, …) is reported as not checked rather than failed.
- Each job sees 4 CPUs, like GitHub's standard runner. A job the kernel kills for want of memory is run again on
  its own; if it still runs out, it is reported as not checked (GitHub's runner has 16 GB) and does not block the push.
  With `--keep-going`, every failure is confirmed by running it alone, since timing checks can fail from sharing.

## Accuracy benchmark

```bash
preflight bench --repo checkstyle/checkstyle          # replay 20 recent PR commits (resumable)
preflight bench --repo checkstyle/checkstyle --report # the report from the results so far
```

For recent PR commits whose CI finished on GitHub, `bench` rebuilds what GitHub tested (the PR merged into the
base branch as it was when CI started), runs the jobs in Docker, and compares each job's pass/fail with GitHub's.
Half the cases are PRs with a failed run, and every job GitHub failed is run, so caught failures are measured
too; passed jobs are sampled (`--per-case`, default 6 job runs per case). Jobs that took over 15 minutes on GitHub
are skipped (`--max-minutes`). It works in its own clone under `~/.preflight/bench/`, never in your checkout.

The report gives agreement overall and split by GitHub's result, missed failures (passed here, failed on
GitHub), false alarms, coverage (GitHub jobs PreFlight can run) and trigger accuracy, and lists every
disagreement with GitHub's job link and the local failing step.

```bash
preflight bench --repo checkstyle/checkstyle --add-failures 8   # add PRs whose code failed a job PreFlight can run
preflight bench --repo checkstyle/checkstyle --replay           # after a fix: run only the disagreeing job runs again
preflight bench --repo checkstyle/checkstyle --replan           # after a verdict change: re-plan saved cases, no jobs run
```

Some results are kept out of the agreement figure and listed on their own, so they neither help nor hurt it:

- **GitHub infrastructure failures**: a job that passed here, where GitHub's own log shows its runner failed (a
  download that returned HTTP 5xx, a dependency that could not be fetched, a machine-setup step such as
  "Install Groovy" failing before the check ran). The code was not what failed.
- **needs-token**: an action GitHub's API refused for want of a token (set `PREFLIGHT_GITHUB_TOKEN`).
- **needs-memory**: killed for memory even with the job running alone (GitHub's runner has 16 GB).

`--add-failures` skips GitHub failures of these kinds before running anything, so the jobs it adds failed on
the code.

## What it does

- **learn**: reads every repo's `.github/workflows`, finds other CI systems (Azure Pipelines, CircleCI, …),
  and samples recent failed PR runs to learn which jobs fail most often. Uses the `gh` CLI for auth.
- **plan / check**: reads the workflow files in your checkout, works out what a PR from this branch
  triggers (events, `types`, `paths` / `paths-ignore`, job `if:`), expands matrices, and runs the
  jobs most likely to fail first.
- **runner**: runs `run:` steps the way the Actions runner does: bash with `-eo pipefail`, `env` at all
  levels, `working-directory`, `if:` with `success()` / `failure()` / `always()`, `continue-on-error`,
  timeouts, and `GITHUB_ENV` / `GITHUB_PATH` / `GITHUB_OUTPUT` between steps. Common actions
  (`checkout`, `cache`, `setup-java`, …) are emulated; machine-setup steps (`sudo apt install …`) are
  skipped, and a missing tool is reported by name.
- **report**: `.git/preflight-report.md`, ready to paste into the PR description.

Every job gets a verdict:

| Verdict | Meaning |
| --- | --- |
| `run` | runs fully here |
| `partial` | runs, with some steps skipped (an action PreFlight does not emulate yet) |
| `other-os` | needs a Windows (or macOS) runner |
| `docker` | needs a job container or service containers; runs with `--docker` |
| `secrets` | needs repository secrets |
| `needs-docker` | a step starts Docker containers itself (Qodana, `docker/build-push-action`, …) |
| `incomplete` | a step that cannot run here (a Docker container action, …) produces outputs later steps check against |
| `timing` | measures run time (performance regression, benchmarks); a laptop cannot reproduce GitHub's timing |
| `pr-metadata` | reads the PR's number or description, so it can only run once the PR exists |
| `reusable`, `dynamic` | calls a reusable workflow, or computes its matrix or runner at run time (phase 2) |
| `no-code` | no `run:` steps: bots, labelers, uploads |

## Known limits (MVP)

- Without `--docker`, steps run directly on your machine and `setup-*` actions only check that the tool exists,
  so every matrix entry uses the same local Python/Java/Node.
- Docker mode runs containers at your machine's architecture (arm64 on Apple silicon); GitHub runs x64.
  The image has common build tools, not everything on GitHub's ~30 GB runner image; a missing tool is reported by name.
- The container's checkout has one commit (like `actions/checkout`'s default `fetch-depth: 1`), so jobs that diff
  against the base branch's history see none.
- `setup-java` always installs Temurin; other `distribution`s are noted in the log.
- Parallel jobs share the Maven/Gradle/npm cache volumes, as jobs on one self-hosted runner would. Native mode
  (no `--docker`) always runs one job at a time, because jobs there share your checkout.
- Only GitHub Actions jobs run. Other CI systems are detected and listed.
- Without `--docker`, marketplace actions PreFlight does not emulate are skipped and the job is marked `partial`.

## Tests

```bash
pip install -e '.[dev]' && pytest -q
```
