// The STATIC fallback footer — what the public renderer shows when the backend has no published footer
// yet, or the fetch fails. It must never leave the page footer-less, so this is a complete, valid,
// bilingual FooterConfig authored in each language's own voice (Arabic native, not a calque of English).
import type { FooterBanner, FooterConfig } from './types';

const noBanner: FooterBanner = {
  enabled: false, message: { ar: '', en: '' }, href: '', tone: 'info', start: '', end: '',
};

export function emptyFooter(): FooterConfig {
  return {
    tagline: { ar: '', en: '' },
    groups: [], socials: [], legal: [],
    contact: { email: '', phone: '' },
    banner: noBanner,
  };
}

// The built-in default. Support address matches the product (KD-33); links are on-site relative paths so
// they resolve behind whichever edge serves the app.
export const STATIC_FOOTER: FooterConfig = {
  tagline: {
    ar: 'الامتثال بلا عناء — من أول إطار إلى تقرير موقّع.',
    en: 'Compliance without the grind — from first framework to signed report.',
  },
  groups: [
    {
      id: 'product', order: 0, hidden: false,
      title: { ar: 'المنصة', en: 'Product' },
      links: [
        { id: 'frameworks', order: 0, hidden: false, newTab: false, url: '/standards',
          label: { ar: 'الأطر', en: 'Frameworks' } },
        { id: 'dashboard', order: 1, hidden: false, newTab: false, url: '/dashboard',
          label: { ar: 'لوحة المتابعة', en: 'Dashboard' } },
      ],
    },
    {
      id: 'company', order: 1, hidden: false,
      title: { ar: 'الشركة', en: 'Company' },
      links: [
        { id: 'signin', order: 0, hidden: false, newTab: false, url: '/login',
          label: { ar: 'تسجيل الدخول', en: 'Sign in' } },
        { id: 'signup', order: 1, hidden: false, newTab: false, url: '/signup',
          label: { ar: 'إنشاء حساب', en: 'Get started' } },
      ],
    },
  ],
  socials: [
    { id: 'mail', kind: 'mail', order: 0, hidden: false, url: 'mailto:support@imtithalak.sa',
      label: { ar: 'راسلنا', en: 'Email us' } },
    { id: 'x', kind: 'x', order: 1, hidden: false, url: 'https://x.com/imtithalak',
      label: { ar: 'إكس', en: 'X' } },
    { id: 'linkedin', kind: 'linkedin', order: 2, hidden: false, url: 'https://www.linkedin.com/company/imtithalak',
      label: { ar: 'لينكدإن', en: 'LinkedIn' } },
  ],
  legal: [
    { id: 'terms', order: 0, hidden: false, slug: 'terms', url: '',
      label: { ar: 'شروط الاستخدام', en: 'Terms of Use' } },
    { id: 'privacy', order: 1, hidden: false, slug: 'privacy', url: '',
      label: { ar: 'سياسة الخصوصية', en: 'Privacy Policy' } },
  ],
  contact: { email: 'support@imtithalak.sa', phone: '' },
  banner: noBanner,
};
