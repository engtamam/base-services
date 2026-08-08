import type { HTMLAttributes } from 'react';
import { cn } from '../lib/cn';

/**
 * Card — the signature panel. Radius from --radius-card, soft elevation, and an optional `interactive`
 * hover that lifts on the accent-tinted glow (shadow-glow) with an accent-tinted border — the premium
 * lift the storefront cards earned, now inherited by every card in every app rather than hand-rolled
 * per page. Paired with <CardTitle>/<CardSubtitle>.
 */
export function Card({
  className,
  interactive,
  ...props
}: HTMLAttributes<HTMLDivElement> & { interactive?: boolean }) {
  return (
    <div
      className={cn(
        'rounded-card border border-border-subtle bg-surface-1 p-5 shadow-sm',
        interactive &&
          'cursor-pointer transition-all duration-base ease-smooth hover:-translate-y-0.5 ' +
            'hover:border-accent/50 hover:shadow-glow focus-ring',
        className,
      )}
      {...props}
    />
  );
}

export function CardTitle({ className, ...props }: HTMLAttributes<HTMLHeadingElement>) {
  return (
    <h2 className={cn('text-lg font-semibold tracking-tight text-text-primary', className)} {...props} />
  );
}

export function CardSubtitle({ className, ...props }: HTMLAttributes<HTMLParagraphElement>) {
  return <p className={cn('mt-1 text-sm text-text-muted', className)} {...props} />;
}
