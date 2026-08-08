import { Badge, type BadgeProps } from './Badge';
import { cn } from '../lib/cn';

/** Map a finding severity → a Badge tone (semantic; danger reserved for high-impact). */
export function severityTone(severity?: string | null): NonNullable<BadgeProps['tone']> {
  const s = (severity ?? '').toLowerCase();
  if (s === 'critical' || s === 'high') return 'danger';
  if (s === 'medium') return 'warning';
  if (s === 'low') return 'info';
  return 'neutral';
}

/** A severity pill (CRITICAL / HIGH / …) with the right semantic tone. */
export function SeverityChip({ severity, className }: { severity?: string | null; className?: string }) {
  const label = (severity ?? 'info').toUpperCase();
  return (
    <Badge tone={severityTone(severity)} className={cn('uppercase tracking-wide', className)}>
      {label}
    </Badge>
  );
}

