import { useState, type FormEvent } from 'react';
import { Button } from '../design-system/Button';
import { Field, Input } from '../design-system/Field';
import { useAuthI18n } from './i18n';

/**
 * ChangePassword — the authenticated "change my own password" screen (identity
 * /api/v1/auth/change-password). Prop-driven: the host wires `onSubmit(old, next)`. The new password
 * is confirmed locally (a mismatch never reaches the server); the server owns the strength policy and
 * its refusal maps to friendly copy through the host.
 */
export interface ChangePasswordProps {
  onSubmit: (oldPassword: string, newPassword: string) => Promise<void>;
  onDone?: () => void;
  mapError?: (err: unknown) => string;
  title?: string;
}

export function ChangePassword({ onSubmit, onDone, mapError, title }: ChangePasswordProps) {
  const { t } = useAuthI18n();
  const [oldp, setOld] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const mismatch = confirm.length > 0 && next !== confirm;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (next !== confirm) { setError(t('auth.pw.mismatch')); return; }
    setBusy(true); setError(null);
    try { await onSubmit(oldp, next); onDone?.(); }
    catch (err) { setError(mapError ? mapError(err) : t('auth.err.ERROR')); }
    finally { setBusy(false); }
  }

  return (
    <div>
      <h2 className="text-2xl font-semibold tracking-tight text-text-primary">{title || t('auth.pw.changeTitle')}</h2>
      <form onSubmit={submit} className="mt-6 flex flex-col gap-3.5">
        <Field label={t('auth.pw.current')} htmlFor="cp-old">
          <Input id="cp-old" type="password" autoComplete="current-password" required
            value={oldp} onChange={(e) => setOld(e.target.value)} />
        </Field>
        <Field label={t('auth.pw.new')} htmlFor="cp-new">
          <Input id="cp-new" type="password" autoComplete="new-password" required
            value={next} onChange={(e) => setNext(e.target.value)} />
        </Field>
        <Field label={t('auth.pw.confirm')} htmlFor="cp-confirm">
          <Input id="cp-confirm" type="password" autoComplete="new-password" required
            value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </Field>
        {mismatch && <p className="text-sm text-danger">{t('auth.pw.mismatch')}</p>}
        {error && <p role="alert" className="text-sm text-danger">{error}</p>}
        <Button type="submit" disabled={busy || mismatch || !next} className="mt-1 w-full">
          {t('auth.pw.save')}
        </Button>
      </form>
    </div>
  );
}
