import { useEffect, useRef, useState } from 'react';
import { cn } from '../lib/cn';

// The readiness Ring — Imtithalak's signature "are we ready?" jewel. A progress arc over a faint track, with a
// tabular value at its centre. The arc animates from 0 on mount (respecting prefers-reduced-motion), and
// its colour shifts with the value (SUCCESS green when healthy, warning mid, danger low) so readiness
// reads at a glance. Pure SVG, theme-token colours, RTL-safe (rotation is symmetric).
export interface RingProps {
  /** 0–100. */
  value: number;
  size?: number;
  stroke?: number;
  /** Big number in the centre; defaults to the rounded percentage. */
  label?: string;
  /** Small caption under the label. */
  caption?: string;
  className?: string;
}

// STATUS, NOT ACTION. A healthy readiness is GOOD, so it wears the success ramp (green); accent is
// reserved for "act on this" (buttons, links), which a status jewel is not. Mid is warning, low is danger.
function toneFor(value: number): string {
  if (value >= 75) return 'var(--success)';
  if (value >= 40) return 'var(--warning)';
  return 'var(--danger)';
}

export function Ring({ value, size = 132, stroke = 10, label, caption, className }: RingProps) {
  const clamped = Math.max(0, Math.min(100, value));
  const [shown, setShown] = useState(0);
  const raf = useRef<number | undefined>(undefined);

  useEffect(() => {
    const reduce =
      typeof window !== 'undefined' &&
      window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    if (reduce) {
      setShown(clamped);
      return;
    }
    const start = performance.now();
    const from = 0;
    const dur = 900;
    const tick = (now: number) => {
      const p = Math.min(1, (now - start) / dur);
      // easeOutCubic
      const eased = 1 - Math.pow(1 - p, 3);
      setShown(from + (clamped - from) * eased);
      if (p < 1) raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
    return () => {
      if (raf.current) cancelAnimationFrame(raf.current);
    };
  }, [clamped]);

  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const offset = c * (1 - shown / 100);

  return (
    <div
      className={cn('relative inline-grid place-items-center', className)}
      style={{ width: size, height: size }}
      role="img"
      aria-label={`${Math.round(clamped)}%`}
    >
      <svg width={size} height={size} className="-rotate-90">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke="var(--surface-3)"
          strokeWidth={stroke}
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={toneFor(clamped)}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={c}
          strokeDashoffset={offset}
          style={{ transition: 'stroke 400ms ease' }}
        />
      </svg>
      <div className="absolute inset-0 grid place-items-center text-center">
        <div>
          <div className="text-2xl font-semibold tabular-nums tracking-tight text-text-primary">
            {label ?? `${Math.round(clamped)}%`}
          </div>
          {caption && <div className="mt-0.5 text-[0.7rem] uppercase tracking-wide text-text-muted">{caption}</div>}
        </div>
      </div>
    </div>
  );
}
