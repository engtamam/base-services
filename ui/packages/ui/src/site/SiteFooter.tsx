// The ONE public footer renderer. The logged-out landing, the in-app footer, AND the operator editor's
// live preview all render THIS component, so what an operator previews is byte-for-byte what a visitor
// sees — no preview/production drift. It is pure and presentational: it renders exactly the FooterConfig
// it is handed and honours `order`, `hidden`, `newTab`, typed socials, and the banner's schedule window.
import type { ReactNode } from 'react';
import {
  Mail, Phone, Globe, Linkedin, Github, Youtube, Instagram, Facebook, Send, MessageCircle, Twitter,
  type LucideIcon,
} from 'lucide-react';
import type { FooterConfig, FooterSocial, Locale, LocaleText, SocialKind } from './types';

const SOCIAL_ICON: Record<SocialKind, LucideIcon> = {
  mail: Mail, phone: Phone, whatsapp: MessageCircle, globe: Globe, x: Twitter, linkedin: Linkedin,
  github: Github, youtube: Youtube, instagram: Instagram, facebook: Facebook, telegram: Send,
};

function txt(v: LocaleText | undefined, locale: Locale): string {
  if (!v) return '';
  return (locale === 'ar' ? v.ar : v.en) || v.en || v.ar || '';
}

const byOrder = <T extends { order: number; hidden: boolean }>(items: T[]): T[] =>
  [...items].filter((i) => !i.hidden).sort((a, b) => a.order - b.order);

/** Is the announcement banner live right now? Enabled, non-empty, and inside its [start, end] window.
 *  `preview` bypasses the window so an operator can see the banner while authoring it out of season. */
export function bannerVisible(cfg: FooterConfig, now: Date, preview = false): boolean {
  const b = cfg.banner;
  if (!b?.enabled) return false;
  if (!b.message?.ar && !b.message?.en) return false;
  if (preview) return true;
  const t = now.getTime();
  if (b.start) {
    const s = Date.parse(b.start);
    if (!Number.isNaN(s) && t < s) return false;
  }
  if (b.end) {
    const e = Date.parse(b.end);
    // an end date with no time means "through that day" — push to end-of-day so a same-day banner shows
    const end = /\d{2}:\d{2}/.test(b.end) ? e : e + 24 * 60 * 60 * 1000 - 1;
    if (!Number.isNaN(e) && t > end) return false;
  }
  return true;
}

const BANNER_TONE: Record<string, string> = {
  info: 'bg-surface-2 text-text-secondary',
  accent: 'bg-accent-soft text-accent-on-soft',
  success: 'bg-success-soft text-success-on-soft',
  warning: 'bg-warning-soft text-warning-on-soft',
};

function SocialChip({ s, locale }: { s: FooterSocial; locale: Locale }) {
  const Icon = SOCIAL_ICON[s.kind] ?? Globe;
  const label = txt(s.label, locale) || s.kind;
  return (
    <a
      href={s.url || '#'}
      target="_blank"
      rel="noreferrer"
      aria-label={label}
      title={label}
      className="grid h-9 w-9 place-items-center rounded-lg border border-border-subtle bg-surface-1 text-text-secondary transition hover:border-border-strong hover:text-text-primary focus-ring"
    >
      {s.image
        ? <img src={s.image} alt="" className="h-5 w-5 object-contain" />
        : <Icon size={16} aria-hidden />}
    </a>
  );
}

export interface SiteFooterProps {
  footer: FooterConfig | null | undefined;
  locale: Locale;
  brandName?: string;
  brandMark?: ReactNode;
  /** injected for tests / previewing a specific moment; defaults to now */
  now?: Date;
  /** show the banner regardless of its schedule window (editor preview) */
  previewBanner?: boolean;
  /** how a legal link's slug becomes an href (client resolves to /legal/{slug}); defaults to that */
  legalHref?: (slug: string) => string;
  className?: string;
}

/** The public footer. Renders nothing structural it wasn't given; safe to pass a fallback config so the
 *  page always has a footer. */
export function SiteFooter({
  footer, locale, brandName, brandMark, now = new Date(), previewBanner = false,
  legalHref = (slug) => `/legal/${slug}`, className = '',
}: SiteFooterProps) {
  if (!footer) return null;
  const groups = byOrder(footer.groups ?? []);
  const socials = byOrder(footer.socials ?? []);
  const legal = byOrder(footer.legal ?? []);
  const tagline = txt(footer.tagline, locale);
  const showBanner = bannerVisible(footer, now, previewBanner);
  const year = now.getFullYear();

  return (
    <footer className={`w-full border-t border-border-subtle bg-surface-0 text-text-secondary ${className}`}>
      {showBanner && (
        <a
          href={footer.banner.href || undefined}
          className={`block w-full px-6 py-2.5 text-center text-sm font-medium ${BANNER_TONE[footer.banner.tone] ?? BANNER_TONE.info} ${footer.banner.href ? 'hover:opacity-90' : 'pointer-events-none'}`}
        >
          {txt(footer.banner.message, locale)}
        </a>
      )}

      <div className="mx-auto flex max-w-6xl flex-col gap-10 px-6 py-12">
        <div className="grid gap-10 sm:grid-cols-2 lg:grid-cols-4">
          {/* brand + tagline + socials */}
          <div className="flex flex-col gap-4 lg:col-span-1">
            <div className="flex items-center gap-2 text-text-primary">
              {brandMark}
              {brandName && <span className="text-base font-semibold tracking-tight">{brandName}</span>}
            </div>
            {tagline && <p className="max-w-xs text-sm leading-relaxed text-text-muted">{tagline}</p>}
            {socials.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {socials.map((s) => <SocialChip key={s.id} s={s} locale={locale} />)}
              </div>
            )}
          </div>

          {/* link columns */}
          {groups.map((g) => {
            const links = byOrder(g.links ?? []);
            if (links.length === 0 && !txt(g.title, locale)) return null;
            return (
              <div key={g.id} className="flex flex-col gap-3">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-text-muted">
                  {txt(g.title, locale)}
                </h3>
                <ul className="flex flex-col gap-2">
                  {links.map((l) => (
                    <li key={l.id}>
                      <a
                        href={l.url || '#'}
                        {...(l.newTab ? { target: '_blank', rel: 'noreferrer' } : {})}
                        className="text-sm text-text-secondary transition hover:text-text-primary focus-ring"
                      >
                        {txt(l.label, locale)}
                      </a>
                    </li>
                  ))}
                </ul>
              </div>
            );
          })}
        </div>

        {/* bottom row: contact, legal, copyright */}
        <div className="flex flex-col gap-4 border-t border-border-subtle pt-6 text-sm text-text-muted sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
            <span>© {year} {brandName}</span>
            {footer.contact.email && (
              <a href={`mailto:${footer.contact.email}`} className="hover:text-text-secondary focus-ring">
                {footer.contact.email}
              </a>
            )}
            {footer.contact.phone && (
              <a href={`tel:${footer.contact.phone}`} className="hover:text-text-secondary focus-ring" dir="ltr">
                {footer.contact.phone}
              </a>
            )}
          </div>
          {legal.length > 0 && (
            <nav className="flex flex-wrap items-center gap-x-4 gap-y-1">
              {legal.map((x) => (
                <a
                  key={x.id}
                  href={x.slug ? legalHref(x.slug) : (x.url || '#')}
                  className="hover:text-text-secondary focus-ring"
                >
                  {txt(x.label, locale)}
                </a>
              ))}
            </nav>
          )}
        </div>
      </div>
    </footer>
  );
}
