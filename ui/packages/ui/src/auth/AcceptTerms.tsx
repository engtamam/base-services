import { useState } from 'react';
import { Button } from '../design-system/Button';
import { DocBlocksView, type DocBlock } from '../components';
import { useAuthI18n } from './i18n';

/**
 * AcceptTerms — the post-auth access gate (compliance #631). After login the host calls
 * GET /api/v1/compliance/me/legal/outstanding; if the list is non-empty the app is BLOCKED behind
 * this screen until every required document is accepted. It walks the outstanding documents one at a
 * time, renders each through the SAME rich block engine the pack was authored in (DocBlocksView — it
 * fills the {{TOKEN}} slots from the document's saved values and shows the reader's language), and
 * calls `onAccept(docId)` → the host wires it to POST /me/legal/{doc_id}/accept.
 *
 * A version bump reappears here automatically: the server reports the doc as outstanding again when
 * its current version no longer matches what the person accepted. Prop-driven, no API import.
 */
export interface OutstandingDoc {
  doc_id: string;
  current_version?: string;
  reason?: 'never_accepted' | 'version_changed' | string;
  /** The rich viewer payload from the backend: bilingual blocks + the placeholder values. */
  view?: { title: string; blocks: DocBlock[]; values?: Record<string, string>; version?: string };
}

export interface AcceptTermsProps {
  outstanding: OutstandingDoc[];
  onAccept: (docId: string) => Promise<void>;
  /** Called once nothing is left to accept — the host lets the user into the app. */
  onDone?: () => void;
  mapError?: (err: unknown) => string;
}

export function AcceptTerms({ outstanding, onAccept, onDone, mapError }: AcceptTermsProps) {
  const { t, locale } = useAuthI18n();
  const [i, setI] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const toMsg = (err: unknown) => (mapError ? mapError(err) : t('auth.err.ERROR'));

  const doc = outstanding[i];
  if (!doc) { onDone?.(); return null; }

  async function accept() {
    setBusy(true); setError(null);
    try {
      await onAccept(doc.doc_id);
      if (i + 1 < outstanding.length) setI(i + 1); else onDone?.();
    } catch (err) { setError(toMsg(err)); } finally { setBusy(false); }
  }

  const values = doc.view?.values || {};
  return (
    <div className="flex flex-col gap-4">
      {outstanding.length > 1 && (
        <p className="text-xs text-text-muted">{t('auth.legal.step')} {i + 1} / {outstanding.length}</p>
      )}
      {doc.reason === 'version_changed' && (
        <p className="rounded-lg bg-accent-soft px-3 py-2 text-sm text-accent-on-soft">
          {t('auth.legal.updated')}
        </p>
      )}
      <div className="max-h-[52vh] overflow-y-auto rounded-lg border border-border-subtle bg-surface-1 p-4">
        {doc.view?.blocks?.length
          ? <DocBlocksView blocks={doc.view.blocks} locale={locale}
              resolve={(k) => values[k]} unfilledLabel={t('auth.legal.unfilled')} />
          : <p className="text-sm text-text-muted">{doc.view?.title || doc.doc_id}</p>}
      </div>
      {error && <p role="alert" className="text-sm text-danger">{error}</p>}
      <Button type="button" onClick={accept} disabled={busy} className="w-full">
        {t('auth.legal.accept')}
      </Button>
    </div>
  );
}
