import { useState, type FormEvent, type ReactNode } from 'react';
import { Button } from '../design-system/Button';
import { Field, Input } from '../design-system/Field';
import { Segmented } from '../design-system/Segmented';
import { useAuthI18n } from './i18n';

/**
 * SignupForm — create an account, prop-driven. A company or an individual: the toggle changes only
 * which fields show (an organization name for a company). No API here — `onSignup` gets a typed
 * payload and the host calls identity-service (or any backend). Strings via the i18n seam; token-only.
 */
export type SignupKind = 'company' | 'individual';

export interface SignupPayload {
  kind: SignupKind;
  name: string;            // person's name
  email: string;
  password: string;
  organization?: string;   // company name (company only)
}

export interface SignupFormProps {
  onSignup: (payload: SignupPayload) => Promise<void>;
  mapError?: (err: unknown) => string;
  footerSlot?: ReactNode;
  title?: string;
  /** Which kinds to offer. Default: both. */
  kinds?: SignupKind[];
}

export function SignupForm({ onSignup, mapError, footerSlot, title, kinds = ['company', 'individual'] }: SignupFormProps) {
  const { t } = useAuthI18n();
  const [kind, setKind] = useState<SignupKind>(kinds[0]);
  const [name, setName] = useState('');
  const [organization, setOrganization] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      await onSignup({ kind, name, email, password, organization: kind === 'company' ? organization : undefined });
    } catch (err) {
      setError(mapError ? mapError(err) : t('auth.err.ERROR'));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h2 className="text-2xl font-semibold tracking-tight text-text-primary">{title || t('auth.createAccount')}</h2>
      {kinds.length > 1 && (
        <div className="mt-5">
          <Segmented<SignupKind>
            value={kind}
            onChange={setKind}
            ariaLabel={t('auth.createAccount')}
            options={([
              { value: 'company' as const, label: t('auth.company') },
              { value: 'individual' as const, label: t('auth.individual') },
            ]).filter((o) => kinds.includes(o.value))}
          />
        </div>
      )}
      <form onSubmit={submit} className="mt-5 flex flex-col gap-3.5">
        {kind === 'company' && (
          <Field label={t('auth.orgName')} htmlFor="signup-org">
            <Input id="signup-org" required value={organization} onChange={(e) => setOrganization(e.target.value)} />
          </Field>
        )}
        <Field label={t('auth.name')} htmlFor="signup-name">
          <Input id="signup-name" required value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label={t('auth.email')} htmlFor="signup-email">
          <Input id="signup-email" type="email" autoComplete="email" required value={email}
            onChange={(e) => setEmail(e.target.value)} />
        </Field>
        <Field label={t('auth.password')} htmlFor="signup-password">
          <Input id="signup-password" type="password" autoComplete="new-password" required
            value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        {error && <p role="alert" className="text-sm text-danger">{error}</p>}
        <Button type="submit" disabled={busy} className="mt-1 w-full">{t('auth.createAccount')}</Button>
      </form>
      {footerSlot && <div className="mt-6 text-sm text-text-muted">{footerSlot}</div>}
    </div>
  );
}
