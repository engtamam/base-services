import type { CSSProperties, HTMLAttributes } from 'react';
import { cn } from '../lib/cn';

// Skeleton — a placeholder block for loading shells. A soft surface tint that PULSES via Tailwind's
// `animate-pulse`, which the global `prefers-reduced-motion` rule (index.css) automatically stills to a
// steady block for users who ask for less motion. Theme-token only. Compose many to shape a loading list
// or card; pass `radius="full"` for an avatar/pill placeholder, `radius="text"` for a line of copy.
export interface SkeletonProps extends HTMLAttributes<HTMLDivElement> {
  /** Convenience sizing — otherwise size it with width/height className utilities. */
  width?: number | string;
  height?: number | string;
  radius?: 'sm' | 'md' | 'card' | 'full' | 'text';
}

const RADIUS = {
  sm: 'rounded-sm',
  md: 'rounded-md',
  card: 'rounded-card',
  full: 'rounded-full',
  text: 'rounded',
} as const;

export function Skeleton({ width, height, radius = 'md', className, style, ...props }: SkeletonProps) {
  const s: CSSProperties = { ...style };
  if (width != null) s.width = width;
  if (height != null) s.height = height;
  return (
    <div
      aria-hidden
      className={cn(
        'animate-pulse bg-surface-2',
        radius === 'text' && 'h-[0.85em]',
        RADIUS[radius],
        className,
      )}
      style={s}
      {...props}
    />
  );
}
