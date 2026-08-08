import type { HTMLAttributes } from 'react';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '../lib/cn';

export const badgeVariants = cva(
  'inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium',
  {
    variants: {
      tone: {
        neutral: 'bg-surface-2 text-text-secondary border border-border-subtle',
        accent: 'bg-accent-soft text-accent-on-soft',
        success: 'bg-success-soft text-success-on-soft',
        warning: 'bg-warning-soft text-warning-on-soft',
        danger: 'bg-danger-soft text-danger-on-soft',
        info: 'bg-info-soft text-info-on-soft',
      },
    },
    defaultVariants: { tone: 'neutral' },
  },
);

export interface BadgeProps
  extends HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, tone, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />;
}
