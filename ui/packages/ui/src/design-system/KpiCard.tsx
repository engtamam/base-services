import type { ReactNode } from 'react';
import { Card } from './Card';
import { cn } from '../lib/cn';

export type KpiTone = 'neutral' | 'accent' | 'success' | 'warning' | 'danger';

const toneText: Record<KpiTone, string> = {
  neutral: 'text-text-primary',
  accent: 'text-accent-on-soft',
  success: 'text-success-on-soft',
  warning: 'text-warning-on-soft',
  danger: 'text-danger-on-soft',
};
const toneIconBg: Record<KpiTone, string> = {
  neutral: 'bg-surface-2 text-text-secondary',
  accent: 'bg-accent-soft text-accent-on-soft',
  success: 'bg-success-soft text-success-on-soft',
  warning: 'bg-warning-soft text-warning-on-soft',
  danger: 'bg-danger-soft text-danger-on-soft',
};

/** A period-over-period change shown beside the metric. `dir` colours it — up=success, down=danger,
 *  flat=muted — but callers can flip `goodDown` for metrics where a fall is good (open findings, cost),
 *  so the colour tracks GOODNESS, not arithmetic sign. `value` is the pre-formatted figure (e.g. "+12%").*/
export interface KpiDelta {
  value: ReactNode;
  dir: 'up' | 'down' | 'flat';
  /** When true, a downward move is the healthy one (fewer findings) → green on down, red on up. */
  goodDown?: boolean;
}

function DeltaPill({ delta }: { delta: KpiDelta }) {
  const good = delta.dir === 'flat' ? null : delta.goodDown ? delta.dir === 'down' : delta.dir === 'up';
  const cls = good === null
    ? 'bg-surface-2 text-text-muted'
    : good
      ? 'bg-success-soft text-success-on-soft'
      : 'bg-danger-soft text-danger-on-soft';
  const arrow = delta.dir === 'up' ? '↑' : delta.dir === 'down' ? '↓' : '→';
  return (
    <span className={cn('inline-flex items-center gap-0.5 rounded-full px-1.5 py-0.5 text-caption font-semibold tabular-nums', cls)}>
      <span aria-hidden>{arrow}</span>
      {delta.value}
    </span>
  );
}

/**
 * KpiCard — a glanceable metric tile for the Dashboard band. STATIC by default (display only); pass
 * `onClick` to make it an interactive filter (e.g. the drift tile → the findings view). `spark` is an
 * optional inline sparkline node; `delta` is an optional trend figure (see KpiDelta) rendered under the
 * value. Everything resolves to theme-config tokens (no hard-coded colour).
 */
export function KpiCard({
  icon,
  label,
  value,
  hint,
  tone = 'neutral',
  onClick,
  spark,
  delta,
  testId,
}: {
  icon: ReactNode;
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: KpiTone;
  onClick?: () => void;
  spark?: ReactNode;
  /** Optional trend/delta slot. Accepts a ready KpiDelta descriptor OR any custom node (e.g. a sparkline
   *  area) — omit it and existing usages are untouched. */
  delta?: KpiDelta | ReactNode;
  testId?: string;
}) {
  const isDelta = (d: unknown): d is KpiDelta =>
    !!d && typeof d === 'object' && 'dir' in (d as Record<string, unknown>);
  return (
    <Card
      interactive={!!onClick}
      onClick={onClick}
      role={onClick ? 'button' : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={
        onClick
          ? (e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                onClick();
              }
            }
          : undefined
      }
      className="flex flex-col gap-3"
      data-testid={testId}
    >
      <div className="flex items-center justify-between">
        <span className={cn('rounded-lg p-2', toneIconBg[tone])}>{icon}</span>
        {spark}
      </div>
      <div>
        <div className="flex items-baseline gap-2">
          <div className={cn('text-3xl font-semibold tabular-nums tracking-tight', toneText[tone])}>
            {value}
          </div>
          {delta != null && (isDelta(delta) ? <DeltaPill delta={delta} /> : <span>{delta}</span>)}
        </div>
        <div className="mt-1 text-sm font-medium text-text-secondary">{label}</div>
        {hint != null && <div className="mt-0.5 text-xs text-text-muted">{hint}</div>}
      </div>
    </Card>
  );
}
