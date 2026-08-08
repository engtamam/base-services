// @base-ui/i18n — the base shared catalog (EN + AR) + the composition helpers a host uses to build its
// translator = base catalog ∪ its own product keys. React-free, no app import. Unknown keys fall back
// to the key itself — the identity default the whole library uses. Adopted from Kobo's proven shape.
export { baseEn, baseAr, baseKeys, type BaseKey } from './catalog';
import { baseEn, baseAr } from './catalog';

export type Locale = 'en' | 'ar';
const BASE: Record<Locale, Record<string, string>> = { en: baseEn, ar: baseAr };

/** base ∪ host dict for a locale. Host keys win on collision. */
export function mergeDicts(locale: Locale, host: Record<string, string> = {}): Record<string, string> {
  return { ...BASE[locale], ...host };
}

/** Build a `t(key)` (base ∪ host) for a locale. Unknown key → the key itself. */
export function makeT(locale: Locale, host: Record<string, string> = {}): (key: string) => string {
  const dict = mergeDicts(locale, host);
  return (key: string) => dict[key] ?? key;
}

/** Set document direction for a locale — call on locale change so RTL flips the whole tree. */
export function applyDir(locale: Locale, el: HTMLElement = document.documentElement): void {
  el.setAttribute('dir', locale === 'ar' ? 'rtl' : 'ltr');
  el.setAttribute('lang', locale);
}
