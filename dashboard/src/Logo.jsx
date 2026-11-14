import React from 'react'

// PreFlight's mark, drawn in the flat style of GitHub's icons: a commit on its line (left), the green
// check it earns, and the check's long arm leaving the grey CI loop through its gap as the push.
export default function Logo({ size = 28, className = '', title = 'PreFlight', tile = true }) {
  return (
    <svg className={`pf-logo ${className}`} width={size} height={size} viewBox="0 0 64 64" role="img" aria-label={title} fill="none">
      {tile && (
        <g className="pf-tile">
          <rect x="1" y="1" width="62" height="62" rx="14" fill="#0d1117" />
          <rect x="1.5" y="1.5" width="61" height="61" rx="13.5" stroke="#30363d" />
        </g>
      )}
      {/* the CI loop, open where the push leaves it */}
      <path className="pf-ring" d="M47.97 28.19A17 17 0 1 1 37.81 18.03" pathLength="100"
        stroke="#6e7681" strokeWidth="3.5" strokeLinecap="round" />
      <g className="pf-flight">
        <path className="pf-path" d="M21 33.5l8 8L52 15" pathLength="100"
          stroke="#e6edf3" strokeWidth="5" strokeLinecap="round" strokeLinejoin="round" />
        <path className="pf-head" d="M43.5 15H52v8.5" pathLength="100"
          stroke="#e6edf3" strokeWidth="5" strokeLinecap="round" strokeLinejoin="round" />
        {/* the commit, on its line */}
        <path className="pf-line" d="M9 33.5h7" stroke="#e6edf3" strokeWidth="2.6" strokeLinecap="round" />
        <circle className="pf-commit" cx="21" cy="33.5" r="4.6" fill="#0d1117" stroke="#e6edf3" strokeWidth="2.6" />
      </g>
    </svg>
  )
}
