import React, { useEffect, useMemo, useState } from 'react'

const LANG = {
  'checkstyle/checkstyle': { lang: 'Java', stack: 'Maven · JDK 17/21 · Error Prone · Pitest' },
  'pallets/flask': { lang: 'Python', stack: 'uv · tox · CPython 3.11–3.15 · PyPy · mypy/pyright' },
  'expressjs/express': { lang: 'JavaScript', stack: 'npm · Node.js 18–26 · mocha · eslint' },
}

const pct = (a, b) => (b ? (100 * a) / b : 0)
const fmt = (a, b) => (b ? `${pct(a, b).toFixed(1)}%` : 'n/a')

function outcome(r) {
  if (r.github === 'infra-failure' || r.local === 'moved-dependency') return 'excluded'
  const agree = (r.local === 'passed' && r.github === 'success') || (r.local === 'failed' && r.github === 'failure')
  return agree ? 'agree' : 'disagree'
}

function Meter({ value, label }) {
  return (
    <div className="meter" role="img" aria-label={`${label}: ${value.toFixed(1)}%`}>
      <div className="meter-fill" style={{ width: `${Math.min(100, value)}%` }} />
      <div className="meter-target" style={{ left: '95%' }} title="95% target" />
    </div>
  )
}

function Totals({ repos }) {
  const t = repos.reduce(
    (s, r) => ({
      compared: s.compared + r.compared,
      agree: s.agree + r.agree,
      failed: s.failed + r.github_failed,
      caught: s.caught + r.github_failed_agree,
      falseAlarms: s.falseAlarms + r.false_alarms,
      cases: s.cases + r.cases,
    }),
    { compared: 0, agree: 0, failed: 0, caught: 0, falseAlarms: 0, cases: 0 },
  )
  const tiles = [
    { k: 'Agreement with GitHub', v: fmt(t.agree, t.compared), sub: `${t.agree} of ${t.compared} job runs` },
    { k: 'Real failures caught', v: fmt(t.caught, t.failed), sub: `${t.caught} of ${t.failed} GitHub failures` },
    { k: 'False alarms', v: String(t.falseAlarms), sub: 'failed here, passed on GitHub' },
    { k: 'Pull requests replayed', v: String(t.cases), sub: `${repos.length} projects, ${new Set(repos.map((r) => LANG[r.repo]?.lang)).size} languages` },
  ]
  return (
    <section className="tiles" aria-label="Totals">
      {tiles.map((x) => (
        <div className="tile" key={x.k}>
          <div className="tile-k">{x.k}</div>
          <div className="tile-v">{x.v}</div>
          <div className="tile-sub">{x.sub}</div>
        </div>
      ))}
    </section>
  )
}

function RepoCard({ r }) {
  const info = LANG[r.repo] || { lang: '', stack: '' }
  const acc = pct(r.agree, r.compared)
  const notRun = Object.entries(r.not_run)
  return (
    <article className="card">
      <header className="card-head">
        <div>
          <a className="repo" href={`https://github.com/${r.repo}`} target="_blank" rel="noreferrer">{r.repo}</a>
          <div className="stack">{info.stack}</div>
        </div>
        {info.lang && <span className={`lang lang-${info.lang.toLowerCase()}`}>{info.lang}</span>}
      </header>
      <div className="big">
        {acc.toFixed(1)}<span>%</span>
      </div>
      <div className="big-sub">agreement · {r.agree}/{r.compared} job runs · {r.cases} PRs</div>
      <Meter value={acc} label="Agreement" />
      <dl className="facts">
        <dt>Jobs GitHub passed</dt><dd>{fmt(r.github_passed_agree, r.github_passed)}</dd>
        <dt>Failures caught</dt><dd>{fmt(r.github_failed_agree, r.github_failed)}</dd>
        <dt>Missed failures</dt><dd>{r.missed_failures}</dd>
        <dt>False alarms</dt><dd>{r.false_alarms}</dd>
        <dt>Coverage</dt><dd>{fmt(...r.coverage)}</dd>
        <dt>Trigger accuracy</dt><dd>{fmt(...r.triggers)}</dd>
      </dl>
      {(r.infra > 0 || r.moved > 0) && (
        <p className="note">
          Not compared: {r.infra > 0 && `${r.infra} GitHub runner failure${r.infra > 1 ? 's' : ''}`}
          {r.infra > 0 && r.moved > 0 && ', '}
          {r.moved > 0 && `${r.moved} replay${r.moved > 1 ? 's' : ''} of a moving dependency`}
        </p>
      )}
      {notRun.length > 0 && (
        <details>
          <summary>Left to GitHub ({notRun.reduce((s, [, n]) => s + n, 0)} job runs)</summary>
          <ul className="notrun">
            {notRun.map(([k, n]) => (
              <li key={k}><span>{k.replace('left-to-github: ', '')}</span><b>{n}</b></li>
            ))}
          </ul>
        </details>
      )}
    </article>
  )
}

function Runs({ repos }) {
  const [repo, setRepo] = useState('all')
  const [which, setWhich] = useState('all')
  const [q, setQ] = useState('')
  const all = useMemo(
    () => repos.flatMap((r) => r.runs.map((x) => ({ ...x, repo: r.repo, outcome: outcome(x) }))),
    [repos],
  )
  const shown = all.filter(
    (x) =>
      (repo === 'all' || x.repo === repo) &&
      (which === 'all' || x.outcome === which || (which === 'failed' && x.github === 'failure')) &&
      (!q || `${x.workflow} ${x.job} ${x.title}`.toLowerCase().includes(q.toLowerCase())),
  )
  return (
    <section className="runs">
      <h2>Every compared job run</h2>
      <div className="filters">
        <select value={repo} onChange={(e) => setRepo(e.target.value)} aria-label="Project">
          <option value="all">All projects</option>
          {repos.map((r) => <option key={r.repo} value={r.repo}>{r.repo}</option>)}
        </select>
        <select value={which} onChange={(e) => setWhich(e.target.value)} aria-label="Outcome">
          <option value="all">All outcomes</option>
          <option value="agree">Agree</option>
          <option value="disagree">Disagree</option>
          <option value="failed">GitHub failed</option>
          <option value="excluded">Not compared</option>
        </select>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search job or PR" aria-label="Search" />
        <span className="count">{shown.length} runs</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>Project</th><th>PR commit</th><th>Job</th><th>GitHub</th><th>PreFlight</th><th>Local time</th></tr>
          </thead>
          <tbody>
            {shown.slice(0, 400).map((x, i) => (
              <tr key={i} className={`o-${x.outcome}`}>
                <td className="mono">{x.repo.split('/')[1]}</td>
                <td>
                  <a className="mono" href={`https://github.com/${x.repo}/commit/${x.sha}`} target="_blank" rel="noreferrer">{x.sha.slice(0, 7)}</a>
                  <div className="title">{x.title}</div>
                </td>
                <td>{x.workflow.replace(/\.ya?ml$/, '')} / {x.job}</td>
                <td><a className={`pill g-${x.github}`} href={x.url} target="_blank" rel="noreferrer">{x.github}</a></td>
                <td><span className={`pill l-${x.local}`} title={x.reason || ''}>{x.local}</span></td>
                <td className="mono num">{x.seconds ? `${Math.round(x.seconds)}s` : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function Method() {
  return (
    <section className="method">
      <h2>How it's measured</h2>
      <ol>
        <li><b>Pick real PRs.</b> Recent pull-request commits whose CI finished on GitHub; half of them with a failed run, so catching failures is measured too.</li>
        <li><b>Rebuild what GitHub tested.</b> The PR head merged into its own base branch as it was when CI started, the same merge commit GitHub builds.</li>
        <li><b>Run the jobs locally.</b> In a fresh Ubuntu container per job, with the workflow's own matrix, setup actions, services and marketplace actions.</li>
        <li><b>Compare job by job.</b> Pass/fail of each job against GitHub's result for the same job name.</li>
      </ol>
      <p>
        Kept out of the figure, and listed: failures of GitHub's own runner (a download that returned HTTP 5xx), and replays
        that install another project's branch tip, which has moved since GitHub ran. Jobs that need Windows/macOS,
        secrets, or the opened PR stay on GitHub and count against coverage, not accuracy.
      </p>
    </section>
  )
}

export default function App() {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  useEffect(() => {
    fetch('./results.json')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setData)
      .catch((e) => setErr(String(e.message || e)))
  }, [])
  const repos = (data?.repos || []).filter((r) => r.compared > 0)
  return (
    <main>
      <header className="top">
        <div className="brand">
          <span className="logo" aria-hidden="true">✓</span>
          <span>PreFlight</span>
        </div>
        <a className="gh" href="https://github.com/sahitya0xsingh/preflight" target="_blank" rel="noreferrer">GitHub</a>
      </header>
      <section className="hero">
        <h1>Run a project's GitHub Actions CI on your laptop, and push only when it passes.</h1>
        <p>
          PreFlight reads a repo's workflows, works out which jobs a change triggers, and runs them in Docker the way
          GitHub's runner does. This page measures how often its verdict matches GitHub's, on real pull requests.
        </p>
        <pre className="cmd">pip install git+https://github.com/sahitya0xsingh/preflight{'\n'}preflight check --docker   # before git push</pre>
      </section>
      {err && <p className="err">Could not load results.json ({err}). Run <code>npm run data</code>.</p>}
      {!data && !err && <p className="loading">Loading results…</p>}
      {repos.length > 0 && (
        <>
          <Totals repos={repos} />
          <section className="cards">{repos.map((r) => <RepoCard key={r.repo} r={r} />)}</section>
          <Method />
          <Runs repos={repos} />
          <footer>Results generated {new Date(data.generated).toLocaleString()} with <code>preflight bench</code>.</footer>
        </>
      )}
    </main>
  )
}
