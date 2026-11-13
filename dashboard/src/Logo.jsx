import React, { useId } from 'react'

// PreFlight's mark. A commit (the dot) clears its checks (the tick) and takes off (the arrow),
// leaving a contrail; around it, the CI loop orbits with a build travelling through it.
// Animated with CSS (see .pf-logo in styles.css); still for people who prefer reduced motion.
export default function Logo({ size = 28, className = '', title = 'PreFlight' }) {
  const id = useId().replace(/:/g, '')
  const g = `pf-g-${id}`, r = `pf-r-${id}`, glow = `pf-glow-${id}`
  return (
    <svg className={`pf-logo ${className}`} width={size} height={size} viewBox="0 0 64 64" role="img" aria-label={title} fill="none">
      <defs>
        <linearGradient id={g} x1="10" y1="52" x2="54" y2="10" gradientUnits="userSpaceOnUse">
          <stop offset="0" style={{ stopColor: 'var(--ink)' }} />
          <stop offset="0.5" style={{ stopColor: 'var(--ice-2)' }} />
          <stop offset="1" style={{ stopColor: 'var(--accent-2)' }} />
        </linearGradient>
        <linearGradient id={r} x1="0" y1="0" x2="64" y2="64" gradientUnits="userSpaceOnUse">
          <stop offset="0" style={{ stopColor: 'var(--ice-2)' }} />
          <stop offset="1" style={{ stopColor: 'var(--accent-2)', stopOpacity: 0.2 }} />
        </linearGradient>
        <radialGradient id={glow} cx="32" cy="32" r="30" gradientUnits="userSpaceOnUse">
          <stop offset="0" style={{ stopColor: 'var(--ice-2)', stopOpacity: 0.28 }} />
          <stop offset="1" style={{ stopColor: 'var(--accent-2)', stopOpacity: 0 }} />
        </radialGradient>
      </defs>

      <circle cx="32" cy="32" r="30" fill={`url(#${glow})`} className="pf-halo" />

      {/* the CI loop, with a build travelling around it */}
      <g className="pf-orbit">
        <circle cx="32" cy="32" r="27" stroke={`url(#${r})`} strokeWidth="1.6" strokeDasharray="5 4.2" />
        <circle cx="32" cy="5" r="2.6" style={{ fill: 'var(--accent-2)' }} />
      </g>

      {/* runway: the steps cleared on the ground */}
      <path d="M17 52h6M29 52h6M41 52h6" stroke="var(--faint)" strokeWidth="2.2" strokeLinecap="round" />

      <g className="pf-flight">
        {/* contrail */}
        <circle cx="11" cy="38" r="1.5" className="pf-trail t1" style={{ fill: 'var(--ice-2)' }} />
        <circle cx="7" cy="42" r="1.1" className="pf-trail t2" style={{ fill: 'var(--ice-2)' }} />
        <circle cx="4" cy="45.5" r="0.8" className="pf-trail t3" style={{ fill: 'var(--ice-2)' }} />
        {/* check → take-off */}
        <path d="M16 33l10 10L50 17" className="pf-path" pathLength="100"
          stroke={`url(#${g})`} strokeWidth="5.4" strokeLinecap="round" strokeLinejoin="round" />
        <path d="M37.5 16.6h13v13" className="pf-head" pathLength="100"
          style={{ stroke: 'var(--accent-2)' }} strokeWidth="5.4" strokeLinecap="round" strokeLinejoin="round" />
        {/* spark at the tip */}
        <path d="M54 9.5l1.2 2.8 2.8 1.2-2.8 1.2-1.2 2.8-1.2-2.8-2.8-1.2 2.8-1.2z" className="pf-spark" style={{ fill: 'var(--ink)' }} />
        {/* the commit */}
        <circle cx="16" cy="33" r="4.6" style={{ fill: 'var(--bg)', stroke: 'var(--ink)' }} strokeWidth="2.6" />
        <circle cx="16" cy="33" r="1.6" className="pf-core" style={{ fill: 'var(--ice-2)' }} />
      </g>
    </svg>
  )
}
