import type { ReactNode } from 'react';
import { cn } from '../lib/cn';

// EmptyState — the "nothing here yet" panel: a muted icon, a title, a supporting line, and an optional
// action slot (usually a <Button>). Centred, theme-token only, RTL-safe (text-center + logical spacing).
// Use it for a zero-result list, a not-yet-started section, a cleared filter — anywhere a blank area
// would otherwise read as broken.
export interface EmptyStateProps {
  icon?: ReactNode;
  title: ReactNode;
  /** A supporting line under the title — a sentence on what this area holds or how to fill it. */
  description?: ReactNode;
  /** Optional action(s) — e.g. a primary <Button>. */
  action?: ReactNode;
  className?: string;
}

export function EmptyState({ icon, title, description, action, className }: EmptyStateProps) {
  return (
    <div className={cn('flex flex-col items-center justify-center gap-3 px-6 py-12 text-center', className)}>
      {icon && (
        <div className="grid h-12 w-12 place-items-center rounded-full bg-surface-2 text-text-muted">
          {icon}
        </div>
      )}
      <div className="flex flex-col gap-1">
        <p className="text-title font-semibold text-text-primary">{title}</p>
        {description != null && (
          <p className="mx-auto max-w-sm text-body text-text-muted">{description}</p>
        )}
      </div>
      {action && <div className="mt-1 flex items-center gap-2">{action}</div>}
    </div>
  );
}
