import { useState, type FormEvent } from 'react';
import { Button } from '../design-system/Button';
import { Field, Input } from '../design-system/Field';
import { useAuthI18n } from './i18n';

/**
 * ForgotPassword — request a reset link/code, prop-driven. The response is DELIBERATELY the same
 * whether or not the address is registered: the host's `onRequest` resolves either way and this shows
 * one neutral "if it exists, we sent it" confirmation, so the screen is not an account-enumeration
 * oracle. Which channel the link arrives over is the backend's concern.
 */
export interface ForgotPasswordProps {
  onRequest: (identifier: string) => Promise<void>;
  /** Optional "back to sign in" slot. */
  footerSlot?: React.ReactNode;
  mapError?: (err: unknown) => string;
  title?: string;
}

export function ForgotPassword({ onRequest, footerSlot, mapError, title }: ForgotPasswordProps) {
  const { t } = useAuthI18n();
  const [identifier, setIdentifier] = useState('');
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true); setError(null);
    try { await onRequest(identifier); setSent(true); }
    catch (err) { setError(mapError ? mapError(err) : t('auth.err.ERROR')); }
    finally { setBusy(false); }
  }

  if (sent) {
    return (
      <div>
        <h2 className="text-2xl font-semibold tracking-tight text-text-primary">{t('auth.forgot.sentTitle')}</h2>
        <p className="mt-2 text-sm text-text-muted">{t('auth.forgot.sentHint')}</p>
        {footerSlot && <div className="mt-6 text-sm text-text-muted">{footerSlot}</div>}
      </div>
    );
  }

  return (
    <div>
      <h2 className="text-2xl font-semibold tracking-tight text-text-primary">{title || t('auth.forgot.title')}</h2>
      <p className="mb-6 mt-1 text-sm text-text-muted">{t('auth.forgot.hint')}</p>
      <form onSubmit={submit} className="mt-6 flex flex-col gap-3.5">
        <Field label={t('auth.identifier')} htmlFor="fp-id">
          <Input id="fp-id" autoComplete="username" required value={identifier}
            onChange={(e) => setIdentifier(e.target.value)} />
        </Field>
        {error && <p role="alert" className="text-sm text-danger">{error}</p>}
        <Button type="submit" disabled={busy || !identifier} className="mt-1 w-full">
          {t('auth.forgot.send')}
        </Button>
      </form>
      {footerSlot && <div className="mt-6 text-sm text-text-muted">{footerSlot}</div>}
    </div>
  );
}
