// The footer/site-content model — the SINGLE shared shape used by the public client renderer AND the
// operator editor's live preview, so the two can never drift. Mirrors services/standards/app/core/site.py.
// Arabic is the native default edition, English its peer: every human string is a `{ ar, en }` pair.

export type Locale = 'en' | 'ar';

export interface LocaleText {
  ar: string;
  en: string;
}

export interface FooterLink {
  id: string;
  label: LocaleText;
  url: string;
  newTab: boolean;
  hidden: boolean;
  order: number;
}

export interface FooterGroup {
  id: string;
  title: LocaleText;
  hidden: boolean;
  order: number;
  links: FooterLink[];
}

// A TYPED social/badge chip — the `kind` drives the icon on the client, so these are not generic links.
export type SocialKind =
  | 'mail' | 'phone' | 'whatsapp' | 'globe' | 'x' | 'linkedin'
  | 'github' | 'youtube' | 'instagram' | 'facebook' | 'telegram';

export const SOCIAL_KINDS: SocialKind[] = [
  'mail', 'phone', 'whatsapp', 'globe', 'x', 'linkedin',
  'github', 'youtube', 'instagram', 'facebook', 'telegram',
];

export interface FooterSocial {
  id: string;
  kind: SocialKind;
  label: LocaleText;
  url: string;
  image?: string; // optional inline data: badge image (validated + size-capped server-side)
  hidden: boolean;
  order: number;
}

export interface FooterLegalLink {
  id: string;
  label: LocaleText;
  slug: string; // a published legal doc slug -> /legal/{slug}
  url: string; // …or an explicit url (one of slug/url is set)
  hidden: boolean;
  order: number;
}

export interface FooterContact {
  email: string;
  phone: string;
}

export type BannerTone = 'info' | 'accent' | 'success' | 'warning';

export interface FooterBanner {
  enabled: boolean;
  message: LocaleText;
  href: string;
  tone: BannerTone;
  start: string; // ISO date/datetime — empty means "no lower bound"
  end: string; // ISO date/datetime — empty means "no upper bound"
}

export interface FooterConfig {
  tagline: LocaleText;
  groups: FooterGroup[];
  socials: FooterSocial[];
  legal: FooterLegalLink[];
  contact: FooterContact;
  banner: FooterBanner;
  // present on a published/version payload, absent on a fresh draft
  version?: number | string;
  content_hash?: string;
  published_at?: string;
}
