import { useState, type FormEvent } from 'react';
import { Button } from '../design-system/Button';
import { Field, Input } from '../design-system/Field';
import { useAuthI18n } from './i18n';

/**
 * OtpVerify — enter the one-time code sent to a phone/email, prop-driven. Pairs with identity's
 * /api/v1/otp/{request,verify}: the host wires `onVerify(code)` and `onResend()`; this holds no API.
 *
 * The code is NEVER shown here — it arrives over the channel, which is the whole point. The field is
 * numeric with a fixed length, and resend is throttled by the SERVER (this only reflects that with a
 * disabled state the host controls via `resendIn`). Errors map to friendly copy through the host.
 */
export interface OtpVerifyProps {
  onVerify: (code: string) => Promise<void>;
  onResend?: () => Promise<void>;
  /** Where the code was sent, shown masked by the host (e.g. "+9665•••4567"). */
  sentTo?: string;
  length?: number;                       // digits; default 6 (matches OTP_CODE_LENGTH)
  /** Seconds until resend is allowed again; while > 0 the resend button is disabled. */
  resendIn?: number;
  mapError?: (err: unknown) => string;
  title?: string;
}

export function OtpVerify({ onVerify, onResend, sentTo, length = 6, resendIn = 0, mapError, title }: OtpVerifyProps) {
  const { t } = useAuthI18n();
  const [code, setCode] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const toMsg = (err: unknown) => (mapError ? mapError(err) : t('auth.err.ERROR'));

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true); setError(null);
    try { await onVerify(code); } catch (err) { setError(toMsg(err)); } finally { setBusy(false); }
  }

  async function resend() {
    if (!onResend || resendIn > 0) return;
    setBusy(true); setError(null);
    try { await onResend(); } catch (err) { setError(toMsg(err)); } finally { setBusy(false); }
  }

  return (
    <div>
      <h2 className="text-2xl font-semibold tracking-tight text-text-primary">{title || t('auth.otp.title')}</h2>
      <p className="mb-6 mt-1 text-sm text-text-muted">
        {t('auth.otp.hint')}{sentTo ? ` ${sentTo}` : ''}
      </p>
      <form onSubmit={submit} className="mt-6 flex flex-col gap-3.5">
        <Field label={t('auth.otp.code')} htmlFor="otp-code">
          <Input id="otp-code" inputMode="numeric" autoComplete="one-time-code" required
            pattern={`[0-9]{${length}}`} maxLength={length} value={code}
            onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, length))} />
        </Field>
        {error && <p role="alert" className="text-sm text-danger">{error}</p>}
        <Button type="submit" disabled={busy || code.length < length} className="mt-1 w-full">
          {t('auth.otp.verify')}
        </Button>
      </form>
      {onResend && (
        <button type="button" onClick={resend} disabled={busy || resendIn > 0}
          className="mt-4 text-sm text-text-muted transition hover:text-text-primary disabled:opacity-60">
          {resendIn > 0 ? `${t('auth.otp.resendIn')} ${resendIn}s` : t('auth.otp.resend')}
        </button>
      )}
    </div>
  );
}
