import React, { useEffect, useMemo, useRef, useState } from 'react'
import { mountCrystal } from './Crystal.js'
import Guide from './Guide.jsx'
import Logo from './Logo.jsx'
import { AetherBg, Cursor, Marquee, Preloader, Title, useTilt } from './Fx.jsx'

const ACCOUNTS = ['sahitya0xsingh', 'Sahitya0805']
const LANG = {
  'checkstyle/checkstyle': ['Java', 'Maven · JDK 17/21 · Error Prone · Pitest'],
  'pallets/flask': ['Python', 'uv · tox · CPython 3.11–3.15 · PyPy'],
  'expressjs/express': ['JavaScript', 'npm · Node.js 18–26 · mocha · eslint'],
  'axios/axios': ['JavaScript', 'npm · Node.js · vitest · eslint · build checks'],
  'pmndrs/zustand': ['React · TypeScript', 'pnpm · React 18/19 · vitest · tsc'],
  'encode/httpx': ['Python', 'pip · CPython 3.9–3.13 · pytest · mypy · ruff'],
  'google/gson': ['Java', 'Maven · JDK 11/17/21 · API compatibility'],
  'FasterXML/jackson-core': ['Java', 'Maven · JDK 8–24 · fuzz-safe parser tests'],
}
const pct = (a, b) => (b ? (100 * a) / b : 0)
const fmt = (a, b, d = 1) => (b ? `${pct(a, b).toFixed(d)}%` : '—')

function outcome(r) {
  if (r.github === 'infra-failure' || r.local === 'moved-dependency') return 'excluded'
  const agree = (r.local === 'passed' && r.github === 'success') || (r.local === 'failed' && r.github === 'failure')
  return agree ? 'agree' : 'disagree'
}

function totals(repos) {
  return repos.reduce(
    (s, r) => ({
      compared: s.compared + r.compared, agree: s.agree + r.agree,
      failed: s.failed + r.github_failed, caught: s.caught + r.github_failed_agree,
      falseAlarms: s.falseAlarms + r.false_alarms, cases: s.cases + r.cases,
    }),
    { compared: 0, agree: 0, failed: 0, caught: 0, falseAlarms: 0, cases: 0 },
  )
}

const reducedMotion = () => typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches

// Counts up once the element scrolls into view.
function Count({ to, decimals = 0, suffix = '' }) {
  const ref = useRef(null)
  const [v, setV] = useState(reducedMotion() ? to : 0)
  useEffect(() => {
    if (reducedMotion()) { setV(to); return }
    const el = ref.current
    let raf = 0
    const io = new IntersectionObserver(([e]) => {
      if (!e.isIntersecting) return
      io.disconnect()
      const t0 = performance.now()
      const step = (now) => {
        const k = Math.min(1, (now - t0) / 1400)
        setV(to * (1 - Math.pow(1 - k, 4)))
        if (k < 1) raf = requestAnimationFrame(step)
      }
      raf = requestAnimationFrame(step)
    }, { threshold: 0.4 })
    io.observe(el)
    return () => { io.disconnect(); cancelAnimationFrame(raf) }
  }, [to])
  return <span ref={ref}>{v.toFixed(decimals)}{suffix}</span>
}

// Fades children in as they enter the viewport.
function Reveal({ children, delay = 0, className = '', tilt = false }) {
  const ref = useRef(null)
  const [shown, setShown] = useState(false)
  useEffect(() => {
    const io = new IntersectionObserver(([e]) => { if (e.isIntersecting) { setShown(true); io.disconnect() } }, { threshold: 0.12 })
    io.observe(ref.current)
    return () => io.disconnect()
  }, [])
  return (
    <div ref={ref} data-tilt={tilt || undefined} className={`reveal ${shown ? 'in' : ''} ${className}`} style={{ transitionDelay: `${delay}ms` }}>
      {tilt && <span className="glare" aria-hidden="true" />}
      {children}
    </div>
  )
}

function Scramble({ text }) {
  // Letters settle from noise into the word, once.
  const [out, setOut] = useState(reducedMotion() ? text : text.replace(/\S/g, '·'))
  useEffect(() => {
    if (reducedMotion()) return
    const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789#%/'
    let frame = 0, raf = 0
    const tick = () => {
      frame++
      setOut(text.split('').map((c, i) => (c === ' ' || i < frame / 2 ? c : chars[(i * 7 + frame) % chars.length])).join(''))
      if (frame / 2 < text.length) raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [text])
  return <span aria-label={text}>{out}</span>
}

function Ring({ value, size = 92 }) {
  const r = size / 2 - 6, c = 2 * Math.PI * r
  return (
    <svg className="ring" width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
      <circle cx={size / 2} cy={size / 2} r={r} className="ring-bg" />
      <circle cx={size / 2} cy={size / 2} r={r} className="ring-fg"
        strokeDasharray={c} strokeDashoffset={c * (1 - value / 100)} transform={`rotate(-90 ${size / 2} ${size / 2})`} />
    </svg>
  )
}

const THEMES = [
  ['ice', 'Glacier', 'linear-gradient(135deg, #8de9ff, #5b7cff)'],
  ['aurora', 'Aurora', 'linear-gradient(135deg, #2ef2b8, #9b6bff)'],
  ['nebula', 'Nebula', 'linear-gradient(135deg, #c38bff, #ff5fb7)'],
  ['solar', 'Solar', 'linear-gradient(135deg, #ffb547, #ff5e7e)'],
  ['paper', 'Paper (light)', 'linear-gradient(135deg, #f6f4ef 50%, #2556e6 50%)'],
]

function useTheme() {
  const [theme, setTheme] = useState(() => {
    try {
      const t = localStorage.getItem('pf-theme')
      return ['ice', 'aurora', 'nebula', 'solar', 'paper'].includes(t) ? t : 'ice'
    } catch { return 'ice' }
  })
  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try { localStorage.setItem('pf-theme', theme) } catch { /* private mode: fine */ }
  }, [theme])
  return [theme, setTheme]
}

function ThemePicker({ theme, setTheme }) {
  return (
    <div className="themes" role="radiogroup" aria-label="Colour theme">
      {THEMES.map(([id, name, c]) => (
        <button key={id} role="radio" aria-checked={theme === id} aria-label={name} title={name}
          className={`sw ${theme === id ? 'on' : ''}`} style={{ '--c': c }} onClick={() => setTheme(id)} />
      ))}
    </div>
  )
}

function Hero({ t, shards, theme }) {
  const canvas = useRef(null)
  const crystal = useRef(null)
  useEffect(() => {
    crystal.current = mountCrystal(canvas.current, shards, { reduced: reducedMotion(), palette: theme })
    return () => crystal.current.dispose()
  }, [shards])
  useEffect(() => { crystal.current?.setPalette(theme) }, [theme])
  return (
    <section className="hero">
      <canvas ref={canvas} className="crystal" aria-label={`3D sphere of ${shards.length} job names, one per compared job run`} />
      <div className="hero-copy">
        <Logo size={84} className="big hero-logo" title="PreFlight logo" />
        <p className="eyebrow"><Scramble text="PREFLIGHT / LOCAL CI THAT AGREES WITH GITHUB" /></p>
        <h1 className="mega"><Count to={pct(t.agree, t.compared)} decimals={1} /><span>%</span></h1>
        <p className="lede">
          Run a project's GitHub Actions CI on your laptop, in Docker, before you push. Replayed on real pull
          requests, PreFlight's verdict matched GitHub's on <b>{t.agree} of {t.compared}</b> job runs.
        </p>
        <div className="meta">
          <span>{t.cases} PULL REQUESTS</span><span>{t.caught}/{t.failed} FAILURES CAUGHT</span><span>{t.falseAlarms} FALSE ALARMS</span>
        </div>
        <div className="cta">
          <a className="btn primary" href="https://github.com/sahitya0xsingh/PreFlight" target="_blank" rel="noreferrer">View on GitHub ↗</a>
          <a className="btn" href="#guide">How to use it</a>
        </div>
      </div>
      <div className="legend" aria-hidden="true">
        <span><i className="d agree" />each label is a real job run · agrees with GitHub</span>
        <span><i className="d disagree" />disagrees</span>
        <span><i className="d excluded" />GitHub's runner failed</span>
      </div>
      <div className="scroll-cue" aria-hidden="true">SCROLL</div>
    </section>
  )
}

function Mine({ repos }) {
  if (!repos.length) return null
  const t = totals(repos)
  const authors = [...new Set(repos.flatMap((r) => r.authors))]
  return (
    <section className="block mine" id="mine">
      <Reveal><p className="kicker">01 — MY OWN PULL REQUESTS</p></Reveal>
      <Reveal delay={80}>
        <Title>Tested on the PRs I sent to open source.</Title>
      </Reveal>
      <Reveal delay={140}>
        <div className="accounts">
          {(authors.length ? authors : ACCOUNTS).map((a) => (
            <a key={a} className="acct" href={`https://github.com/${a}`} target="_blank" rel="noreferrer">
              <img src={`https://github.com/${a}.png?size=64`} alt="" width="28" height="28" loading="lazy" />@{a}
            </a>
          ))}
        </div>
      </Reveal>
      <div className="mine-grid">
        <Reveal className="glass stat-xl" tilt>
          <div className="k">Agreement on my PRs</div>
          <div className="v"><Count to={pct(t.agree, t.compared)} decimals={1} suffix="%" /></div>
          <div className="s">{t.agree}/{t.compared} job runs · {t.cases} PRs · {repos.length} repos</div>
        </Reveal>
        {repos.map((r, i) => (
          <Reveal key={r.repo} delay={60 * i} className="glass mini" tilt>
            <a className="repo" href={`https://github.com/${r.repo}/pulls?q=author%3A${authors[0] || ACCOUNTS[0]}`} target="_blank" rel="noreferrer">{r.repo}</a>
            <div className="mini-row">
              <Ring value={pct(r.agree, r.compared)} size={64} />
              <div>
                <div className="mini-v">{fmt(r.agree, r.compared)}</div>
                <div className="s">{r.agree}/{r.compared} runs · {r.cases} PRs</div>
              </div>
            </div>
          </Reveal>
        ))}
      </div>
    </section>
  )
}

function Projects({ repos }) {
  return (
    <section className="block" id="projects">
      <Reveal><p className="kicker">02 — BENCHMARK ACROSS LANGUAGES</p></Reveal>
      <Reveal delay={80}>
        <Title>{`Random recent PRs, ${repos.length} projects, ${new Set(repos.map((r) => (LANG[r.repo] || [''])[0].split(' · ').pop())).size} languages, one verdict.`}</Title>
      </Reveal>
      <div className="cards">
        {repos.map((r, i) => {
          const [lang, stack] = LANG[r.repo] || ['', '']
          const acc = pct(r.agree, r.compared)
          return (
            <Reveal key={r.repo} delay={90 * i} className="glass card" tilt>
              <div className="card-top">
                <span className="tag">{lang}</span>
                <a className="repo" href={`https://github.com/${r.repo}`} target="_blank" rel="noreferrer">{r.repo} ↗</a>
              </div>
              <div className="card-mid">
                <Ring value={acc} />
                <div>
                  <div className="card-v"><Count to={acc} decimals={1} suffix="%" /></div>
                  <div className="s">{r.agree}/{r.compared} job runs · {r.cases} PRs</div>
                </div>
              </div>
              <div className="stack">{stack}</div>
              <dl className="facts">
                <dt>Failures caught</dt><dd>{fmt(r.github_failed_agree, r.github_failed)}</dd>
                <dt>False alarms</dt><dd>{r.false_alarms}</dd>
                <dt>Missed failures</dt><dd>{r.missed_failures}</dd>
                <dt>Runnable on a Mac</dt><dd>{fmt(...r.coverage)}</dd>
                <dt>Trigger accuracy</dt><dd>{fmt(...r.triggers)}</dd>
              </dl>
            </Reveal>
          )
        })}
      </div>
    </section>
  )
}

const STEPS = [
  ['Pick real PRs', 'Recent pull-request commits whose CI finished on GitHub, half with a failed run, so catching failures is measured too.'],
  ['Rebuild the merge', "The PR head merged into its own base branch as it stood when CI started: the commit GitHub actually tested."],
  ['Run it in Docker', "A fresh Ubuntu container per job, with the workflow's matrix, setup actions, services, artifacts and marketplace actions."],
  ['Compare job by job', "Each job's pass/fail against GitHub's result for the job of the same name. Every row links to GitHub's own log."],
]

function Method() {
  return (
    <section className="block" id="method">
      <Reveal><p className="kicker">03 — METHOD</p></Reveal>
      <Title>No synthetic tests. Real PRs, replayed.</Title>
      <ol className="steps">
        {STEPS.map(([h, p], i) => (
          <Reveal key={h} delay={90 * i} className="step">
            <span className="num">0{i + 1}</span>
            <h3>{h}</h3>
            <p>{p}</p>
          </Reveal>
        ))}
      </ol>
      <Reveal>
        <p className="fine">
          Kept out of the figure, and listed: failures of GitHub's own runner (a download that returned HTTP 5xx) and
          replays that install another project's moving branch. Jobs that need Windows, macOS, secrets or the opened PR
          stay on GitHub and count against coverage, not accuracy.
        </p>
      </Reveal>
    </section>
  )
}

function Ledger({ repos }) {
  const [repo, setRepo] = useState('all')
  const [which, setWhich] = useState('all')
  const [q, setQ] = useState('')
  const all = useMemo(
    () => repos.flatMap((r) => r.runs.map((x) => ({ ...x, repo: r.repo, set: r.set, outcome: outcome(x) }))),
    [repos],
  )
  const names = [...new Set(repos.map((r) => r.repo))]
  const shown = all.filter((x) =>
    (repo === 'all' || x.repo === repo) &&
    (which === 'all' || (which === 'mine' ? x.set === 'authors' : which === 'failed' ? x.github === 'failure' : x.outcome === which)) &&
    (!q || `${x.workflow} ${x.job} ${x.title} ${x.sha}`.toLowerCase().includes(q.toLowerCase())))
  return (
    <section className="block" id="ledger">
      <Reveal><p className="kicker">04 — THE LEDGER</p></Reveal>
      <Title>Every compared job run.</Title>
      <div className="filters">
        <select value={repo} onChange={(e) => setRepo(e.target.value)} aria-label="Project">
          <option value="all">All projects</option>
          {names.map((n) => <option key={n} value={n}>{n}</option>)}
        </select>
        <select value={which} onChange={(e) => setWhich(e.target.value)} aria-label="Outcome">
          <option value="all">All runs</option>
          <option value="mine">My PRs</option>
          <option value="agree">Agree</option>
          <option value="disagree">Disagree</option>
          <option value="failed">GitHub failed</option>
          <option value="excluded">Not compared</option>
        </select>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search job, PR or commit" aria-label="Search" />
        <span className="count">{shown.length} RUNS</span>
      </div>
      <div className="glass table-wrap">
        <table>
          <thead><tr><th>Project</th><th>PR commit</th><th>Job</th><th>GitHub</th><th>PreFlight</th><th className="num">Local</th></tr></thead>
          <tbody>
            {shown.slice(0, 500).map((x, i) => (
              <tr key={i} className={`o-${x.outcome}`}>
                <td className="mono">{x.repo.split('/')[1]}{x.set === 'authors' && <span className="me">ME</span>}</td>
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

export default function App() {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const [theme, setTheme] = useTheme()
  useTilt()
  useEffect(() => {
    fetch('./results.json')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setData)
      .catch((e) => setErr(String(e.message || e)))
  }, [])
  const all = (data?.repos || []).filter((r) => r.compared > 0)
  const sample = all.filter((r) => r.set !== 'authors')
  const mine = all.filter((r) => r.set === 'authors')
  const t = totals(all)
  const shards = useMemo(
    () => all.flatMap((r) => r.runs.map((x) => ({
      o: r.set === 'authors' && outcome(x) === 'agree' ? 'mine' : outcome(x),
      label: x.job.length > 22 ? x.job.slice(0, 21) + '…' : x.job,
    }))),
    [data],
  )
  return (
    <>
      <AetherBg />
      <Preloader ready={!!data || !!err} />
      <Cursor />
      <div className="grain" aria-hidden="true" />
      <svg width="0" height="0" style={{ position: 'absolute' }} aria-hidden="true">
        <defs>
          <linearGradient id="ring-grad" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" style={{ stopColor: 'var(--ice-2)' }} />
            <stop offset="1" style={{ stopColor: 'var(--accent-2)' }} />
          </linearGradient>
        </defs>
      </svg>
      <nav className="nav">
        <a className="brand" href="#top"><Logo size={30} /><span className="brand-name">PREFLIGHT</span></a>
        <div className="links">
          {mine.length > 0 && <a href="#mine">My PRs</a>}
          <a href="#projects">Benchmark</a>
          <a href="#method">Method</a>
          <a href="#ledger">Ledger</a>
          <a href="#guide">Guide</a>
          <ThemePicker theme={theme} setTheme={setTheme} />
          <a className="gh" href="https://github.com/sahitya0xsingh/PreFlight" target="_blank" rel="noreferrer">GitHub ↗</a>
        </div>
      </nav>
      <main id="top">
        {err && <p className="status err">Could not load results.json ({err}).</p>}
        {!data && !err && <p className="status">Loading results…</p>}
        {all.length > 0 && (
          <>
            <Hero t={t} shards={shards} theme={theme} />
            <Marquee items={[...new Set(all.flatMap((r) => r.runs.map((x) => x.job)))].slice(0, 40)} />
            <Mine repos={mine} />
            <Projects repos={sample} />
            <Method />
            <Ledger repos={all} />
            <Marquee items={['preflight plan', 'preflight check --docker', 'preflight install-hook', 'preflight bench', 'git push', '✓ green', 'docker run', 'merge commit', 'job by job']} />
            <Guide Reveal={Reveal} Title={Title} />
            <footer>
              <span>Built by <a href="https://github.com/sahitya0xsingh" target="_blank" rel="noreferrer">Sahitya Singh</a></span>
              <span className="mono">pip install git+https://github.com/sahitya0xsingh/PreFlight</span>
              <span>Data from <code>preflight bench</code>, {new Date(data.generated).toLocaleDateString()}</span>
            </footer>
          </>
        )}
      </main>
    </>
  )
}
