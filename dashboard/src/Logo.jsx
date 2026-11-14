import React from 'react'

// PreFlight's mark: a folded paper plane leaving a pencil-dashed trail. Flat paper tones, one crease,
// no effects, so it reads like something drawn by hand and holds up at favicon size.
export default function Logo({ size = 28, className = '', title = 'PreFlight', tile = true }) {
  return (
    <svg className={`pf-logo ${className}`} width={size} height={size} viewBox="0 0 64 64" role="img" aria-label={title} fill="none">
      {tile && <rect className="pf-tile" x="1" y="1" width="62" height="62" rx="15" fill="#121a26" />}

      {/* the trail it leaves */}
      <path className="pf-trail" d="M9 55c4-6 7-9 11.5-10.5" stroke="#8fa3bf" strokeWidth="2.4"
        strokeLinecap="round" strokeDasharray="0.1 5.2" />

      <g className="pf-flight">
        {/* upper wing */}
        <path d="M53 12 9.5 29.5l14 6.2z" fill="#f3efe6" strokeLinejoin="round" stroke="#f3efe6" strokeWidth="1.2" />
        {/* lower wing, in its own shadow */}
        <path d="M53 12 23.5 35.7 31 52.5z" fill="#c8c0af" strokeLinejoin="round" stroke="#c8c0af" strokeWidth="1.2" />
        {/* the keel folded under */}
        <path d="M23.5 35.7 26.2 46 31 52.5z" fill="#9c937f" strokeLinejoin="round" stroke="#9c937f" strokeWidth="1.2" />
        {/* centre crease */}
        <path d="M53 12 23.5 35.7" stroke="#121a26" strokeOpacity="0.35" strokeWidth="1" strokeLinecap="round" />
      </g>
    </svg>
  )
}
