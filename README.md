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
| `docker` | needs a job container or service containers (phase 2) |
| `secrets` | needs repository secrets |
| `pr-metadata` | reads the PR's number or description, so it can only run once the PR exists |
| `reusable`, `dynamic` | calls a reusable workflow, or computes its matrix or runner at run time (phase 2) |
| `no-code` | no `run:` steps: bots, labelers, uploads |

## Known limits (MVP)

- Steps run directly on your machine, not in a container. Docker isolation is phase 2.
- `setup-*` actions only check that the tool exists (and the Java major version); they do not switch versions,
  so every matrix entry uses the same local Python/Java/Node.
- Only GitHub Actions jobs run. Other CI systems are detected and listed.
- Unknown marketplace actions are skipped, and the job is marked `partial`.

## Tests

```bash
pip install -e '.[dev]' && pytest -q
```
