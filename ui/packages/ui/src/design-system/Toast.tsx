import { useSyncExternalStore } from 'react';
import { Check } from 'lucide-react';

// Minimal, dependency-free toast: a module-level store + a <Toaster/> host mounted once (AppShell).
// `toast(msg)` can be called from anywhere (no context plumbing) — used for instant feedback like the
// "Machine ID copied" confirmation. Auto-dismisses; RTL-safe (logical inset, centered).

interface Toast {
  id: number;
  message: string;
}

let toasts: Toast[] = [];
const listeners = new Set<() => void>();
let seq = 0;

function emit() {
  for (const l of listeners) l();
}

/** Show a transient toast. Returns the toast id (rarely needed). */
export function toast(message: string, ttlMs = 2200): number {
  const id = ++seq;
  toasts = [...toasts, { id, message }];
  emit();
  setTimeout(() => {
    toasts = toasts.filter((t) => t.id !== id);
    emit();
  }, ttlMs);
  return id;
}

function subscribe(cb: () => void) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}
function snapshot() {
  return toasts;
}

/** Mount once near the app root. Renders the active toasts, bottom-centered, above everything. */
export function Toaster() {
  const items = useSyncExternalStore(subscribe, snapshot, snapshot);
  if (items.length === 0) return null;
  return (
    <div
      className="pointer-events-none fixed inset-x-0 bottom-6 z-50 flex flex-col items-center gap-2"
      data-testid="toaster"
    >
      {items.map((t) => (
        <div
          key={t.id}
          role="status"
          data-testid="toast"
          className="pointer-events-auto flex items-center gap-2 rounded-lg border border-border-strong bg-surface-2 px-4 py-2 text-sm font-medium text-text-primary shadow-lg"
        >
          <Check size={16} className="text-success-on-soft" />
          {t.message}
        </div>
      ))}
    </div>
  );
}
