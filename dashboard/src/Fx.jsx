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

// Intro: the logo, a counter to 100, then a frosted curtain lifts. Skipped for reduced motion.
export function Preloader({ ready }) {
  const [n, setN] = useState(0)
  const [gone, setGone] = useState(reducedMotion())
  useEffect(() => {
    if (gone) return
    let raf = 0
    const t0 = performance.now()
    const tick = (now) => {
      const k = Math.min(1, (now - t0) / 1500)
      setN(Math.round(100 * (1 - Math.pow(1 - k, 3))))
      if (k < 1 || !ready) raf = requestAnimationFrame(tick)
      else setTimeout(() => setGone(true), 900)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [ready, gone])
  if (gone) return null
  const done = n >= 100 && ready
  return (
    <div className={`preloader ${done ? 'lift' : ''}`} aria-hidden="true">
      <div className="pre-inner">
        <Logo size={72} className="big" />
        <div className="pre-count">{String(n).padStart(3, '0')}</div>
        <div className="pre-line"><i style={{ transform: `scaleX(${n / 100})` }} /></div>
        <div className="pre-cap">REPLAYING PULL REQUESTS</div>
      </div>
    </div>
  )
}

// A soft cursor: a dot that follows exactly and a ring that trails and swells over links.
export function Cursor() {
  const dot = useRef(null), ring = useRef(null)
  useEffect(() => {
    if (reducedMotion() || matchMedia('(pointer: coarse)').matches) return
    document.documentElement.classList.add('has-cursor')
    let x = innerWidth / 2, y = innerHeight / 2, rx = x, ry = y, raf = 0
    const move = (e) => {
      x = e.clientX; y = e.clientY
      const hot = e.target.closest?.('a, button, select, input, [data-tilt]')
      ring.current.classList.toggle('hot', !!hot)
    }
    const loop = () => {
      rx += (x - rx) * 0.14; ry += (y - ry) * 0.14
      dot.current.style.transform = `translate(${x}px, ${y}px)`
      ring.current.style.transform = `translate(${rx}px, ${ry}px)`
      raf = requestAnimationFrame(loop)
    }
    addEventListener('pointermove', move, { passive: true })
    loop()
    return () => { cancelAnimationFrame(raf); removeEventListener('pointermove', move); document.documentElement.classList.remove('has-cursor') }
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
        <span className="w" key={i} aria-hidden="true">
          <span style={{ transitionDelay: `${i * 55}ms` }}>{w}</span>{' '}
        </span>
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
