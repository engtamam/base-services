import type { ReactNode } from 'react';

/**
 * AuthLayout — the split brand / pane shell every auth screen sits in.
 *
 * PURE and PROP-DRIVEN: it imports no app. The host supplies the brand side (`brand`), the pane
 * content (`children`), and an optional `footer`. Token-only styling, RTL-safe (logical properties),
 * so it themes with everything else and mirrors in Arabic with no change.
 */
export interface AuthLayoutProps {
  children: ReactNode;
  /** The left/brand half — a logo, a tagline, an illustration. Hidden on small screens. */
  brand?: ReactNode;
  /** Optional footer under the pane (e.g. the shared SiteFooter, or links). */
  footer?: ReactNode;
}

export function AuthLayout({ children, brand, footer }: AuthLayoutProps) {
  return (
    <div className="min-h-dvh w-full bg-surface-0 text-text-primary">
      <div className="mx-auto grid min-h-dvh max-w-6xl grid-cols-1 lg:grid-cols-2">
        {/* brand side */}
        <aside className="relative hidden overflow-hidden lg:block">
          <div className="absolute inset-0 bg-accent-soft/40" aria-hidden />
          <div className="relative flex h-full flex-col justify-center gap-6 p-12">{brand}</div>
        </aside>
        {/* pane side */}
        <main className="flex flex-col justify-center px-6 py-12 sm:px-12">
          <div className="mx-auto w-full max-w-sm">{children}</div>
          {footer && <div className="mx-auto mt-10 w-full max-w-sm">{footer}</div>}
        </main>
      </div>
    </div>
  );
}
