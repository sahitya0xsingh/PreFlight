import React, { useId } from 'react'

// PreFlight's mark: a commit (the dot) clears its checks (the tick), and the tick's long arm leaves the
// CI loop (the ring) through its one gap: the push taking off. A dark glass tile keeps it legible from
// favicon size up. Animated in CSS (.pf-logo); static for reduced motion.
export default function Logo({ size = 28, className = '', title = 'PreFlight', tile = true }) {
  const id = useId().replace(/:/g, '')
  const g = `pf-g-${id}`, r = `pf-r-${id}`, t = `pf-t-${id}`, e = `pf-e-${id}`, s = `pf-s-${id}`
  return (
    <svg className={`pf-logo ${className}`} width={size} height={size} viewBox="0 0 64 64" role="img" aria-label={title} fill="none">
      <defs>
        <linearGradient id={g} x1="18" y1="44" x2="54" y2="12" gradientUnits="userSpaceOnUse">
          <stop offset="0" style={{ stopColor: 'var(--mega-1)' }} />
          <stop offset="0.55" style={{ stopColor: 'var(--ice)' }} />
          <stop offset="1" style={{ stopColor: 'var(--accent-2)' }} />
        </linearGradient>
        <linearGradient id={r} x1="14" y1="52" x2="50" y2="16" gradientUnits="userSpaceOnUse">
          <stop offset="0" style={{ stopColor: 'var(--accent-2)', stopOpacity: 0.15 }} />
          <stop offset="1" style={{ stopColor: 'var(--ice)', stopOpacity: 0.9 }} />
        </linearGradient>
        <linearGradient id={t} x1="0" y1="0" x2="0" y2="64" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#111a29" />
          <stop offset="1" stopColor="#05070b" />
        </linearGradient>
        <linearGradient id={e} x1="0" y1="4" x2="64" y2="60" gradientUnits="userSpaceOnUse">
          <stop offset="0" style={{ stopColor: 'var(--ice)', stopOpacity: 0.45 }} />
          <stop offset="0.5" style={{ stopColor: 'var(--ice)', stopOpacity: 0.06 }} />
          <stop offset="1" style={{ stopColor: 'var(--accent-2)', stopOpacity: 0.3 }} />
        </linearGradient>
        <radialGradient id={s} cx="44" cy="20" r="30" gradientUnits="userSpaceOnUse">
          <stop offset="0" style={{ stopColor: 'var(--ice)', stopOpacity: 0.22 }} />
          <stop offset="1" style={{ stopColor: 'var(--ice)', stopOpacity: 0 }} />
        </radialGradient>
      </defs>

      {tile && (
        <g className="pf-tile">
          <rect x="2" y="2" width="60" height="60" rx="17" fill={`url(#${t})`} />
          <rect x="2" y="2" width="60" height="60" rx="17" fill={`url(#${s})`} />
          <rect x="2.5" y="2.5" width="59" height="59" rx="16.5" stroke={`url(#${e})`} />
        </g>
      )}

      {/* the CI loop, open where the push leaves it */}
      <path className="pf-ring" d="M47.97 28.19A17 17 0 1 1 37.81 18.03" pathLength="100"
        stroke={`url(#${r})`} strokeWidth="3" strokeLinecap="round" />

      <g className="pf-flight">
        {/* the checks passing, then take-off */}
        <path className="pf-path" d="M21 33.5l8 8L52 15" pathLength="100"
          stroke={`url(#${g})`} strokeWidth="5" strokeLinecap="round" strokeLinejoin="round" />
        <path className="pf-head" d="M43.5 15H52v8.5" pathLength="100"
          style={{ stroke: 'var(--accent-2)' }} strokeWidth="5" strokeLinecap="round" strokeLinejoin="round" />
        {/* the commit */}
        <circle className="pf-commit" cx="21" cy="33.5" r="4.2" style={{ fill: '#070b12', stroke: 'var(--mega-1)' }} strokeWidth="2.4" />
      </g>
    </svg>
  )
}
