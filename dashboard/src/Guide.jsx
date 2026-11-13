import React, { useState } from 'react'

// A step-by-step guide: tabs for each workflow, numbered steps, terminal blocks with copy buttons
// and the output you should expect.
const TABS = [
  {
    id: 'start',
    name: 'Quick start',
    blurb: 'Check a branch before you push it, in about two minutes of setup.',
    steps: [
      {
        title: 'Install',
        text: 'Python 3.10+, git and the GitHub CLI (`gh auth login`) are all you need. Docker is optional but recommended.',
        cmd: 'pip install git+https://github.com/sahitya0xsingh/preflight',
      },
      {
        title: 'Learn the org once',
        text: "Reads every repo's workflows and recent failed PR runs, so the jobs most likely to fail run first.",
        cmd: 'preflight learn checkstyle',
        out: 'Scanned 12 repos · 41 workflows · profile saved to ~/.preflight/profiles/checkstyle.json',
      },
      {
        title: 'See what your change triggers',
        text: 'From inside your checkout, on your PR branch. Nothing runs yet: it works out events, paths filters, job `if:` and the matrix.',
        cmd: 'cd ~/checkstyle\npreflight plan',
        out: 'Will run (14 job runs):\n  - CI / build  x4 matrix  [failed 6x on recent PRs]\n  - Checker Framework / checker-framework\nCannot run here (GitHub will run these):\n  - CI / windows: other-os: needs a Windows runner',
      },
      {
        title: 'Run it',
        text: 'Each job runs the way GitHub would, and it stops at the first failure with the log tail of the failing step.',
        cmd: 'preflight check --docker',
        out: '  ✓ CI / build (17) passed in 212s\n  ✗ CI / checkstyle failed in 48s: \'mvn verify\' exit 1\n✗ 1 of 14 job runs failed. Fix them before pushing.',
      },
    ],
  },
  {
    id: 'docker',
    name: 'Docker mode',
    blurb: "Fresh Ubuntu containers per job: your machine stays clean and results match GitHub's runner.",
    steps: [
      {
        title: 'Start Docker',
        text: 'Docker Desktop (or any daemon). Give it at least 8 GB of memory; heavy Maven jobs like more.',
        cmd: 'docker info --format "{{.MemTotal}}"',
      },
      {
        title: 'Run in parallel',
        text: 'Jobs run up to 4 at a time, each with 4 CPUs like a GitHub runner. A job starts once the jobs it `needs` passed; artifacts pass between them.',
        cmd: 'preflight check --docker -j 3',
      },
      {
        title: 'Run everything, even after a failure',
        text: 'Every failure is then re-run alone to confirm it, so a timing check that failed from sharing the CPU is not reported.',
        cmd: 'preflight check --docker --keep-going',
      },
      {
        title: 'Only some jobs',
        text: 'Match by job id, workflow name or file name. `--quick` runs only the first matrix entry of each job.',
        cmd: 'preflight check --docker --only checkstyle error-prone --quick',
      },
      {
        title: 'Give jobs a GitHub token (optional)',
        text: "Some actions call GitHub's API. Use a classic token with no scopes: it can read public data and nothing else. PreFlight never reads your gh login for this.",
        cmd: 'read -s PREFLIGHT_GITHUB_TOKEN && export PREFLIGHT_GITHUB_TOKEN',
      },
    ],
  },
  {
    id: 'hook',
    name: 'Git hook',
    blurb: 'Make it automatic: every `git push` checks first, and a red result stops the push.',
    steps: [
      {
        title: 'Install the pre-push hook',
        text: 'In hook mode only committed code is checked, so what runs is exactly what you push.',
        cmd: 'preflight install-hook --docker',
      },
      {
        title: 'Push as usual',
        text: 'Green: the push goes through. Red: the push stops and the failing step is printed.',
        cmd: 'git push origin my-branch',
      },
      {
        title: 'Skip it once',
        text: 'For a docs-only change or an emergency.',
        cmd: 'PREFLIGHT_SKIP=1 git push',
      },
      {
        title: 'Paste the report into your PR',
        text: 'Each check writes a Markdown summary you can paste into the PR description.',
        cmd: 'cat .git/preflight-report.md',
      },
    ],
  },
  {
    id: 'bench',
    name: 'Benchmark',
    blurb: "Measure PreFlight on any public repo: it replays recent PRs and compares every job with GitHub's result.",
    steps: [
      {
        title: 'Replay 20 recent PRs',
        text: 'Rebuilds the merge commit GitHub tested, runs the jobs in Docker, compares pass/fail. Resumable: run it again to continue.',
        cmd: 'preflight bench --repo pallets/flask --cases 20 -j 3',
        out: '**Agreement: 100.0% (117/117)** (target 95%: met)',
        real: true,
      },
      {
        title: 'Only your own PRs',
        text: 'Pass one or more GitHub logins. Results are kept apart from the random sample.',
        cmd: 'preflight bench --repo checkstyle/checkstyle --authors your-login,your-other-login',
      },
      {
        title: 'After a fix, replay only the disagreements',
        text: 'Seconds instead of hours.',
        cmd: 'preflight bench --repo pallets/flask --replay',
      },
      {
        title: 'Publish the numbers',
        text: 'Writes every benchmarked repo to one JSON file; this dashboard reads it.',
        cmd: 'preflight bench --export dashboard/public/results.json',
      },
    ],
  },
]

function Terminal({ cmd, out, real }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    navigator.clipboard?.writeText(cmd).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1400) })
  }
  return (
    <div className="term">
      <div className="term-bar">
        <span className="dots" aria-hidden="true"><i /><i /><i /></span>
        <button className="copy" onClick={copy} aria-label="Copy command">{copied ? 'COPIED' : 'COPY'}</button>
      </div>
      <pre>
        {cmd.split('\n').map((l, i) => <div key={i}><span className="ps">$</span> {l}</div>)}
        {out && <div className="out"><span className="out-k">{real ? 'real output' : 'example output'}</span>{'\n'}{out}</div>}
      </pre>
    </div>
  )
}

const md = (s) => s.split(/(`[^`]+`)/).map((p, i) => (p.startsWith('`') ? <code key={i}>{p.slice(1, -1)}</code> : p))

export default function Guide({ Reveal, Title }) {
  const [tab, setTab] = useState('start')
  const cur = TABS.find((t) => t.id === tab)
  return (
    <section className="block" id="guide">
      <Reveal><p className="kicker">05 — GUIDE</p></Reveal>
      <Title>From install to a green push.</Title>
      <Reveal delay={120}>
        <div className="tabs" role="tablist">
          {TABS.map((t) => (
            <button key={t.id} role="tab" aria-selected={tab === t.id} className={tab === t.id ? 'on' : ''} onClick={() => setTab(t.id)}>
              {t.name}
            </button>
          ))}
        </div>
      </Reveal>
      <p className="tab-blurb">{md(cur.blurb)}</p>
      <ol className="guide" key={tab}>
        {cur.steps.map((s, i) => (
          <li key={s.title} className="g-step" style={{ animationDelay: `${i * 70}ms` }}>
            <div className="g-num">{String(i + 1).padStart(2, '0')}</div>
            <div className="g-body">
              <h3>{s.title}</h3>
              <p>{md(s.text)}</p>
              <Terminal cmd={s.cmd} out={s.out} real={s.real} />
            </div>
          </li>
        ))}
      </ol>
    </section>
  )
}
