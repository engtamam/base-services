import { useMemo, useState } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { Button } from './Button';
import { useImtithalakUi } from './shell';

// ── Pager ──────────────────────────────────────────────────────────────────────────────────────────
// A tiny, reusable client-side pager so long tables page (10/page by default) instead of scrolling to
// infinity. `usePaged` slices the array + tracks the current page (auto-clamped when the set shrinks —
// e.g. after a filter); `<Pager/>` renders the Prev · "Page N of M" · Next footer (RTL-aware, i18n'd,
// hidden when there's only one page). Same look/keys as the Findings table so every list reads alike.
//
// The Prev/Next/label strings come from the host via the injected shell seam (`useImtithalakUi().t`) — so the
// shared component carries no app i18n import, yet on-prem renders the exact same translated labels.

const DEFAULT_PAGE_SIZE = 10;

export interface Paged<T> {
  page: number;
  setPage: React.Dispatch<React.SetStateAction<number>>;
  pageCount: number;
  pageItems: T[];
  total: number;
  pageSize: number;
}

/** Paginate `items` client-side. The returned `page` is clamped to the valid range so a shrinking list
 *  (after filtering/deletion) never strands the user on an empty page. */
export function usePaged<T>(items: T[], pageSize: number = DEFAULT_PAGE_SIZE): Paged<T> {
  const [page, setPage] = useState(0);
  const pageCount = Math.max(1, Math.ceil(items.length / pageSize));
  const clamped = Math.min(Math.max(0, page), pageCount - 1);
  const pageItems = useMemo(
    () => items.slice(clamped * pageSize, clamped * pageSize + pageSize),
    [items, clamped, pageSize],
  );
  return { page: clamped, setPage, pageCount, pageItems, total: items.length, pageSize };
}

/** The Prev · "Page N of M" · Next footer. Renders nothing when there is a single page. */
export function Pager({
  page,
  setPage,
  pageCount,
  testId = 'pager',
  className,
}: {
  page: number;
  setPage: React.Dispatch<React.SetStateAction<number>>;
  pageCount: number;
  testId?: string;
  className?: string;
}) {
  const { t } = useImtithalakUi();
  if (pageCount <= 1) return null;
  return (
    <div
      className={className ?? 'flex items-center justify-end gap-2 px-4 py-2.5 text-xs text-text-muted'}
      data-testid={testId}
    >
      <Button
        variant="ghost"
        size="sm"
        disabled={page <= 0}
        onClick={() => setPage((p) => Math.max(0, p - 1))}
        data-testid="page-prev"
        aria-label={t('pager.prev')}
      >
        <ChevronLeft size={15} className="rtl:rotate-180" /> {t('pager.prev')}
      </Button>
      <span className="tabular-nums" data-testid="page-indicator">
        {t('pager.label')} {page + 1} {t('pager.of')} {pageCount}
      </span>
      <Button
        variant="ghost"
        size="sm"
        disabled={page >= pageCount - 1}
        onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
        data-testid="page-next"
        aria-label={t('pager.next')}
      >
        {t('pager.next')} <ChevronRight size={15} className="rtl:rotate-180" />
      </Button>
    </div>
  );
}
