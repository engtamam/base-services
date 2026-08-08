import { cn } from '../lib/cn';

// The precious Seal — the wax-seal / certificate emblem that marks a sealed binder or published report as
// something valuable and immutable (KD-17). A concentric medallion with a subtle radial sheen, a notched
// rim, and a checkmark at its heart; an optional version chip. Pure SVG + token colours, so it inherits
// the accent and both themes. Deliberately understated — precious, not loud.
export interface SealProps {
  size?: number;
  /** e.g. "v2" — rendered as a small chip on the rim. Omit to hide. */
  version?: string;
  /** Muted (grey) rendering for a draft/unsealed state. */
  muted?: boolean;
  className?: string;
}

export function Seal({ size = 64, version, muted, className }: SealProps) {
  const ink = muted ? 'var(--text-muted)' : 'var(--accent)';
  const notches = Array.from({ length: 24 }, (_, i) => (i * 360) / 24);
  const r = size / 2;

  return (
    <div className={cn('relative inline-grid place-items-center', className)} style={{ width: size, height: size }} aria-hidden>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <defs>
          <radialGradient id="sealSheen" cx="38%" cy="32%" r="75%">
            <stop offset="0%" stopColor={ink} stopOpacity={muted ? 0.14 : 0.26} />
            <stop offset="60%" stopColor={ink} stopOpacity={muted ? 0.06 : 0.12} />
            <stop offset="100%" stopColor={ink} stopOpacity={0.04} />
          </radialGradient>
        </defs>
        {/* notched rim */}
        {notches.map((deg) => {
          const a = (deg * Math.PI) / 180;
          const x = r + Math.cos(a) * (r - 2);
          const y = r + Math.sin(a) * (r - 2);
          return <circle key={deg} cx={x} cy={y} r={size * 0.012} fill={ink} opacity={0.55} />;
        })}
        <circle cx={r} cy={r} r={r - size * 0.08} fill="url(#sealSheen)" stroke={ink} strokeWidth={size * 0.02} strokeOpacity={0.5} />
        <circle cx={r} cy={r} r={r - size * 0.16} fill="none" stroke={ink} strokeWidth={size * 0.012} strokeOpacity={0.35} />
        {/* checkmark at the heart */}
        <path
          d={`M ${r - size * 0.13} ${r + size * 0.01} l ${size * 0.09} ${size * 0.09} l ${size * 0.17} -${size * 0.2}`}
          fill="none"
          stroke={ink}
          strokeWidth={size * 0.05}
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      {version && (
        <span
          className="absolute -bottom-1 rounded-pill border border-border-subtle bg-surface-1 px-1.5 text-[0.6rem] font-semibold tabular-nums text-text-secondary shadow-sm"
        >
          {version}
        </span>
      )}
    </div>
  );
}
