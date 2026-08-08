import { useState, type FormEvent, type ReactNode } from 'react';
import { ArrowLeft, Building2 } from 'lucide-react';
import { Button } from '../design-system/Button';
import { Field, Input } from '../design-system/Field';
import { useAuthI18n } from './i18n';

/**
 * LoginForm — sign in, prop-driven. No "organization" field: the person proves who they are (email +
 * password) and the BACKEND decides which organizations they may enter (T1). One org → the host routes
 * straight in; several → this shows an org picker.
 *
 * The library holds no API: `onLogin` returns `{ done }` (host routed in) or `{ orgs }` (show picker),
 * and `onChooseOrg` sets the session. Errors are mapped to friendly copy by the host via `mapError`
 * (so a stable backend CODE never leaks); strings come through the i18n seam.
 */
export interface LoginOrg { tenant: string; label?: string; roles?: string[] }

export interface LoginFormProps {
  onLogin: (username: string, password: string) => Promise<{ done: boolean; orgs?: LoginOrg[] }>;
  onChooseOrg: (tenant: string) => Promise<void>;
  /** Turn a thrown error into a friendly line. Defaults to a generic message via the i18n seam. */
  mapError?: (err: unknown) => string;
  /** Optional slot under the form (e.g. a "Create account" link). */
  footerSlot?: ReactNode;
  /** Optional heading/subheading overrides; default to i18n keys. */
  title?: string;
  subtitle?: string;
}

export function LoginForm({ onLogin, onChooseOrg, mapError, footerSlot, title, subtitle }: LoginFormProps) {
  const { t } = useAuthI18n();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [orgs, setOrgs] = useState<LoginOrg[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const toMsg = (err: unknown) => (mapError ? mapError(err) : t('auth.err.ERROR'));

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      const step = await onLogin(username, password);
      if (!step.done && step.orgs && step.orgs.length > 0) setOrgs(step.orgs);
    } catch (err) {
      setError(toMsg(err));
    } finally {
      setBusy(false);
    }
  }

  async function pick(tenant: string) {
    setBusy(true); setError(null);
    try {
      await onChooseOrg(tenant);
    } catch (err) {
      setError(toMsg(err));
    } finally {
      setBusy(false);
    }
  }

  if (orgs) {
    return (
      <div>
        <button type="button" onClick={() => { setOrgs(null); setError(null); }}
          className="mb-4 inline-flex items-center gap-1 text-sm text-text-muted transition hover:text-text-primary">
          <ArrowLeft size={15} /> {t('auth.back')}
        </button>
        <h2 className="text-2xl font-semibold tracking-tight text-text-primary">{t('auth.pickOrg')}</h2>
        <p className="mb-6 mt-1 text-sm text-text-muted">{t('auth.pickOrgHint')}</p>
        <div className="flex flex-col gap-2">
          {orgs.map((m) => (
            <button key={m.tenant} type="button" onClick={() => pick(m.tenant)} disabled={busy}
              className="group flex items-center gap-3 rounded-lg border border-border-subtle bg-surface-1 px-4 py-3 text-start transition hover:border-accent hover:bg-surface-2/40 disabled:opacity-60">
              <span className="grid h-10 w-10 shrink-0 place-items-center rounded-lg bg-accent-soft text-accent-on-soft">
                <Building2 size={18} />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium text-text-primary">{m.label || m.tenant}</span>
                {m.roles && m.roles.length > 0 && (
                  <span className="block truncate text-xs text-text-muted">{m.roles.join(' · ')}</span>
                )}
              </span>
            </button>
          ))}
        </div>
        {error && <p role="alert" className="mt-4 text-sm text-danger">{error}</p>}
      </div>
    );
  }

  return (
    <div>
      <h2 className="text-2xl font-semibold tracking-tight text-text-primary">{title || t('auth.signIn')}</h2>
      {subtitle && <p className="mb-6 mt-1 text-sm text-text-muted">{subtitle}</p>}
      <form onSubmit={submit} className="mt-6 flex flex-col gap-3.5">
        <Field label={t('auth.email')} htmlFor="login-email">
          <Input id="login-email" type="email" autoComplete="username" required value={username}
            onChange={(e) => setUsername(e.target.value)} />
        </Field>
        <Field label={t('auth.password')} htmlFor="login-password">
          <Input id="login-password" type="password" autoComplete="current-password" required
            value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        {error && <p role="alert" className="text-sm text-danger">{error}</p>}
        <Button type="submit" disabled={busy} className="mt-1 w-full">{t('auth.signIn')}</Button>
      </form>
      {footerSlot && <div className="mt-6 text-sm text-text-muted">{footerSlot}</div>}
    </div>
  );
}
