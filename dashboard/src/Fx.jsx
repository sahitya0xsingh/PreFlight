import React, { useEffect, useRef, useState } from 'react'
import { mountAether } from './Aether.js'
import Logo from './Logo.jsx'

export const reducedMotion = () =>
  typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches

// Fullscreen fog behind everything.
export function AetherBg() {
  const ref = useRef(null)
  useEffect(() => mountAether(ref.current, { reduced: reducedMotion() }), [])
  return <canvas ref={ref} className="aether" aria-hidden="true" />
}

// Intro: a pre-flight check. A four-stage pipeline lights up while a CI-style log streams real job runs
// from the benchmark (✓ agreed with GitHub, ✗ did not); then the mark's arrow takes off toward the top-right
// corner and the screen wipes away behind it. Once per browser session; skipped for reduced motion.
const STAGES = ['Checkout', 'Setup', 'Test', 'Compare']
const FALLBACK = ['build', 'lint', 'tests (3.12)', 'typing', 'coverage', 'checkstyle', 'error-prone', 'Node.js 22']

function seenIntro() {
  try { return sessionStorage.getItem('pf-intro') === '1' } catch { return false }
}

export function Preloader({ ready, runs = [], total = 0 }) {
  const [k, setK] = useState(0)                 // 0..1 progress
  const [phase, setPhase] = useState(reducedMotion() || seenIntro() ? 'gone' : 'run')   // run → launch → gone
  const start = useRef(0)
  useEffect(() => {
    if (phase !== 'run') return
    let raf = 0
    if (!start.current) start.current = performance.now()   // keep the clock when `ready` flips mid-way
    const t0 = start.current
    const tick = (now) => {
      const p = Math.min(1, (now - t0) / 2100)
      setK(p)
      if (p < 1 || !ready) { raf = requestAnimationFrame(tick); return }
      try { sessionStorage.setItem('pf-intro', '1') } catch { /* private mode */ }
      setTimeout(() => setPhase('launch'), 180)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [ready, phase])
  useEffect(() => {
    if (phase !== 'launch') return
    const t = setTimeout(() => setPhase('gone'), 1300)
    return () => clearTimeout(t)
  }, [phase])
  if (phase === 'gone') return null

  const ease = 1 - Math.pow(1 - k, 2)
  const lines = runs.length ? runs : FALLBACK.map((job) => ({ repo: 'preflight', job, o: 'agree', s: 0 }))
  const shown = Math.min(lines.length, Math.floor(ease * lines.length * 0.6) + 1)
  const log = lines.slice(Math.max(0, shown - 7), shown)
  const stageAt = ease * STAGES.length
  return (
    <div className={`preloader ${phase}`} aria-hidden="true">
      <div className="pre-grid" />
      <div className="pre-streak" />
      <div className="pre-inner">
        <div className="pre-mark"><Logo size={88} className="big intro" /></div>

        <div className="pre-pipe">
          <div className="pre-track"><i style={{ transform: `scaleX(${Math.min(1, ease * 1.04)})` }} /></div>
          {STAGES.map((st, i) => {
            const state = stageAt >= i + 1 ? 'done' : stageAt >= i ? 'live' : ''
            return (
              <div key={st} className={`pre-node ${state}`} style={{ left: `${(i / (STAGES.length - 1)) * 100}%` }}>
                <span className="dot">{state === 'done' ? '✓' : ''}</span>
                <span className="lbl">{st}</span>
              </div>
            )
          })}
        </div>

        <div className="pre-log">
          {log.map((x, i) => (
            <div key={shown - log.length + i} className={`pre-ln ${x.o}`} style={{ opacity: 0.25 + (0.75 * (i + 1)) / log.length }}>
              <span className="m">{x.o === 'agree' ? '✓' : x.o === 'disagree' ? '✗' : '•'}</span>
              <span className="r">{x.repo}</span>
              <span className="j">{x.job}</span>
              <span className="t">{x.s ? `${Math.round(x.s)}s` : ''}</span>
            </div>
          ))}
        </div>

        <div className="pre-row">
          <span>{phase === 'launch' ? 'Cleared for take-off' : 'Pre-flight check'}</span>
          <span className="pre-count">{Math.round(ease * total)}<em>/{total || '—'} job runs</em></span>
        </div>
      </div>
    </div>
  )
}

// A soft cursor: a dot that follows exactly and a ring that trails and swells over links.
export function Cursor() {
  const dot = useRef(null), ring = useRef(null)
  useEffect(() => {
    if (reducedMotion() || !matchMedia('(pointer: fine)').matches) return
    document.documentElement.classList.add('has-cursor')
    let x = -100, y = -100, rx = x, ry = y, raf = 0, seen = false
    const show = (on) => { dot.current.classList.toggle('on', on); ring.current.classList.toggle('on', on) }
    const move = (e) => {
      x = e.clientX; y = e.clientY
      if (!seen) { seen = true; rx = x; ry = y }   // first move: appear where the pointer is, not mid-screen
      show(true)
      const t = e.target
      ring.current.classList.toggle('hot', !!t.closest?.('a, button, select, [role=tab], [data-tilt]'))
      ring.current.classList.toggle('text', !!t.closest?.('input, textarea'))
    }
    const leave = () => show(false)
    const down = () => ring.current.classList.add('down')
    const up = () => ring.current.classList.remove('down')
    const loop = () => {
      rx += (x - rx) * 0.2; ry += (y - ry) * 0.2
      dot.current.style.transform = `translate3d(${x}px, ${y}px, 0)`
      ring.current.style.transform = `translate3d(${rx}px, ${ry}px, 0)`
      raf = requestAnimationFrame(loop)
    }
    addEventListener('pointermove', move, { passive: true })
    addEventListener('pointerdown', down, { passive: true })
    addEventListener('pointerup', up, { passive: true })
    document.documentElement.addEventListener('mouseleave', leave)
    addEventListener('blur', leave)
    loop()
    return () => {
      cancelAnimationFrame(raf)
      removeEventListener('pointermove', move); removeEventListener('pointerdown', down); removeEventListener('pointerup', up)
      document.documentElement.removeEventListener('mouseleave', leave); removeEventListener('blur', leave)
      document.documentElement.classList.remove('has-cursor')
    }
  }, [])
  return (
    <>
      <div ref={ring} className="cur-ring" aria-hidden="true" />
      <div ref={dot} className="cur-dot" aria-hidden="true" />
    </>
  )
}

// Headings rise word by word from behind a mask when they scroll in.
export function Title({ children, as: Tag = 'h2' }) {
  const ref = useRef(null)
  const [on, setOn] = useState(reducedMotion())
  useEffect(() => {
    const io = new IntersectionObserver(([e]) => { if (e.isIntersecting) { setOn(true); io.disconnect() } }, { threshold: 0.3 })
    io.observe(ref.current)
    return () => io.disconnect()
  }, [])
  const words = String(children).split(' ')
  return (
    <Tag ref={ref} className={`title-split ${on ? 'on' : ''}`} aria-label={String(children)}>
      {words.map((w, i) => (
        <React.Fragment key={i}>
          <span className="w" aria-hidden="true">
            <span style={{ transitionDelay: `${i * 55}ms` }}>{w}</span>
          </span>
          {i < words.length - 1 ? ' ' : null}
        </React.Fragment>
      ))}
    </Tag>
  )
}

// Cards tilt toward the pointer, with a glare that follows it.
export function useTilt() {
  useEffect(() => {
    if (reducedMotion() || matchMedia('(pointer: coarse)').matches) return
    const move = (e) => {
      const el = e.target.closest?.('[data-tilt]')
      document.querySelectorAll('[data-tilt].tilting').forEach((x) => {
        if (x !== el) { x.classList.remove('tilting'); x.style.transform = '' }
      })
      if (!el) return
      const r = el.getBoundingClientRect()
      const px = (e.clientX - r.left) / r.width, py = (e.clientY - r.top) / r.height
      el.classList.add('tilting')
      el.style.transform = `perspective(900px) rotateX(${(0.5 - py) * 7}deg) rotateY(${(px - 0.5) * 9}deg) translateY(-4px)`
      el.style.setProperty('--gx', `${px * 100}%`)
      el.style.setProperty('--gy', `${py * 100}%`)
    }
    addEventListener('pointermove', move, { passive: true })
    return () => removeEventListener('pointermove', move)
  }, [])
}

// A slow frosted band of real job names between sections.
export function Marquee({ items }) {
  const list = items.length ? items : ['preflight']
  const row = [...list, ...list]
  return (
    <div className="marquee" aria-hidden="true">
      <div className="marquee-track">
        {row.map((t, i) => <span key={i}>{t}<i>◆</i></span>)}
      </div>
    </div>
  )
}
