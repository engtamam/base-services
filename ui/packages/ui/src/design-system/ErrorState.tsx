import type { ReactNode } from 'react';
import { AlertTriangle } from 'lucide-react';
import { cn } from '../lib/cn';

// ErrorState — the "this didn't load" panel. Same shape as EmptyState but tinted for a failure and built
// around a Retry: pass `onRetry` for the default retry button, or a custom `action` node. Theme-token only,
// RTL-safe, keyboard-accessible (the retry button is a real <button>). Icon defaults to a danger triangle.
export interface ErrorStateProps {
  icon?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  /** Wire a retry handler to get the default retry button. */
  onRetry?: () => void;
  /** Label for the default retry button (supply localised copy). Defaults to "Retry". */
  retryLabel?: ReactNode;
  /** A custom action node, used INSTEAD of the default retry button when provided. */
  action?: ReactNode;
  className?: string;
}

export function ErrorState({
  icon, title, description, onRetry, retryLabel = 'Retry', action, className,
}: ErrorStateProps) {
  return (
    <div
      role="alert"
      className={cn('flex flex-col items-center justify-center gap-3 px-6 py-12 text-center', className)}
    >
      <div className="grid h-12 w-12 place-items-center rounded-full bg-danger-soft text-danger-on-soft">
        {icon ?? <AlertTriangle size={22} />}
      </div>
      <div className="flex flex-col gap-1">
        <p className="text-title font-semibold text-text-primary">{title}</p>
        {description != null && (
          <p className="mx-auto max-w-sm text-body text-text-muted">{description}</p>
        )}
      </div>
      {action ?? (onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-1 inline-flex items-center gap-2 rounded-md border border-border-strong bg-transparent px-4 py-2 text-sm font-medium text-text-primary transition-colors outline-none hover:bg-surface-2 focus-ring"
        >
          {retryLabel}
        </button>
      ))}
    </div>
  );
}
