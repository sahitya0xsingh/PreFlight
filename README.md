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
- Jobs run in parallel: by default about one per 2.5 GB of Docker memory, up to 4 (`--jobs N` / `-j N` to change).
  A job starts once every job it `needs` has passed. The first failure stops the running containers and cancels the
  rest, unless you pass `--keep-going`; with it, jobs that need a failed job are reported as not run.
- Jobs that need Windows or macOS runners, secrets, or the PR itself are still left to GitHub.

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
- Unknown marketplace actions are skipped, and the job is marked `partial`.

## Tests

```bash
pip install -e '.[dev]' && pytest -q
```
