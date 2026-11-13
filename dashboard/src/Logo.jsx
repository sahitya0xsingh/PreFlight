import React from 'react'

// PreFlight's mark: a commit (the dot) runs its checks (the tick) and takes off (the arrow) —
// over a runway of dashes, the CI steps it passed on the ground.
export default function Logo({ className = 'logo-mark', title = 'PreFlight' }) {
  return (
    <svg className={className} viewBox="0 0 32 32" role="img" aria-label={title} fill="none">
      <defs>
        <linearGradient id="pf-g" x1="4" y1="26" x2="28" y2="5" gradientUnits="userSpaceOnUse">
          <stop offset="0" style={{ stopColor: 'var(--ink)' }} />
          <stop offset="1" style={{ stopColor: 'var(--ice-2)' }} />
        </linearGradient>
      </defs>
      <path d="M5.5 28.5h4M13 28.5h4M20.5 28.5h4" stroke="var(--faint)" strokeWidth="1.8" strokeLinecap="round" />
      <path d="M6.5 15.5l6 6L26 7.5" stroke="url(#pf-g)" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M18.6 7.2H26.3v7.7" stroke="var(--ice-2)" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="6.5" cy="15.5" r="2.9" fill="var(--bg)" stroke="var(--ink)" strokeWidth="2" />
    </svg>
  )
}
