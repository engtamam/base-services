import { useEffect, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { cn } from '../lib/cn';

/**
 * Drawer — the ONE consistent right-side slide-over the whole app uses for detail (findings, schedules,
 * assets). Dependency-free (React portal + theme tokens): a dimmed backdrop, Escape to close, a focusable
 * panel, and a calm slide using the config motion vars. RTL-aware (opens from the logical end side).
 */
export function Drawer({
  open,
  onClose,
  title,
  subtitle,
  children,
  footer,
  testId,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  subtitle?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  testId?: string;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', onKey);
      document.body.style.overflow = prev;
    };
  }, [open, onClose]);

  if (!open || typeof document === 'undefined') return null;

  return createPortal(
    <div className="fixed inset-0 z-50" role="dialog" aria-modal="true" data-testid={testId}>
      {/* backdrop */}
      <button
        type="button"
        aria-label="Close"
        onClick={onClose}
        className="absolute inset-0 cursor-default bg-[rgb(2_6_15_/_0.45)] backdrop-blur-[1px]"
      />
      {/* panel — pinned to the logical end (right in LTR, left in RTL) */}
      <div
        className={cn(
          'absolute inset-y-0 end-0 flex w-full max-w-md flex-col border-s border-border-strong',
          'bg-surface-1 shadow-pop',
        )}
      >
        <header className="flex items-start justify-between gap-3 border-b border-border-subtle p-5">
          <div className="min-w-0">
            <h2 className="truncate text-lg font-semibold tracking-tight text-text-primary">{title}</h2>
            {subtitle != null && <p className="mt-0.5 text-sm text-text-muted">{subtitle}</p>}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded-md p-1.5 text-text-muted transition-colors hover:bg-surface-2 hover:text-text-primary focus-ring"
          >
            <X size={18} />
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto p-5">{children}</div>
        {footer != null && (
          <footer className="flex items-center gap-2 border-t border-border-subtle p-4">{footer}</footer>
        )}
      </div>
    </div>,
    document.body,
  );
}
