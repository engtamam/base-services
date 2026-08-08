// A working example that mounts the whole kit — so an adopter copies from something real, and so a
// typecheck of THIS file verifies every @base-ui export resolves and composes.
import { useEffect, useState } from 'react';
import { applyTheme } from '@base-ui/theme';
import type { Appearance } from '@base-ui/theme';
import { makeT, applyDir, type Locale } from '@base-ui/i18n';
import { Button } from '@base-ui/design-system/Button';
import { Card } from '@base-ui/design-system/Card';
import { Badge } from '@base-ui/design-system/Badge';
import { DocBlocksView, type DocBlock } from '@base-ui/components';
import { AuthLayout, LoginForm, AuthI18nProvider } from '@base-ui/auth';

const DOC: DocBlock[] = [
  { cover: { title: 'Service Agreement', subtitle: 'Between {{party_a}} and {{party_b}}' } },
  { section: { heading: '1. Scope' } },
  { clause: { text: 'The provider shall deliver **{{service}}** by {{date}}.' } },
];

export function App() {
  const [locale, setLocale] = useState<Locale>('en');
  const [appearance, setAppearance] = useState<Appearance>('light');
  const [brand, setBrand] = useState(false);
  const t = makeT(locale);

  useEffect(() => {
    applyTheme(appearance, brand ? { 'brand.accent': '#0F5F4E' } : {});
    applyDir(locale);
  }, [locale, appearance, brand]);

  return (
    <AuthI18nProvider value={{ t, locale }}>
      <div className="min-h-dvh bg-surface-0 p-8 text-text-primary">
        <div className="mx-auto flex max-w-3xl flex-col gap-6">
          <header className="flex items-center justify-between">
            <h1 className="text-2xl font-semibold">@base-ui demo</h1>
            <div className="flex gap-2">
              <Button variant="soft" onClick={() => setLocale(locale === 'en' ? 'ar' : 'en')}>{locale.toUpperCase()}</Button>
              <Button variant="ghost" onClick={() => setAppearance(appearance === 'light' ? 'dark' : 'light')}>{appearance}</Button>
              <Button variant="outline" onClick={() => setBrand((b) => !b)}>Brand</Button>
            </div>
          </header>

          <Card className="p-6">
            <div className="mb-3 flex items-center gap-2"><Badge>paper editor</Badge></div>
            {/* The on-page paper editor: read-only here; pass `edit` ops to make it Word-like in place. */}
            <DocBlocksView
              blocks={DOC}
              locale={locale}
              resolve={(k) => ({ party_a: 'Acme', service: 'Support' } as Record<string, string>)[k]}
              unfilledLabel="(to fill)"
            />
          </Card>

          <Card className="p-0 overflow-hidden">
            <AuthLayout brand={<h2 className="text-xl font-semibold">Welcome</h2>}>
              <LoginForm
                onLogin={async () => ({ done: true })}
                onChooseOrg={async () => {}}
              />
            </AuthLayout>
          </Card>
        </div>
      </div>
    </AuthI18nProvider>
  );
}
