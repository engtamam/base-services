/**
 * theme.config.ts — THE SINGLE SOURCE OF TRUTH for the whole client UI's look.
 *
 * Edit ONE value here and the entire app changes: colours, surfaces, the accent, radii, shadows,
 * typography scale, motion. Nothing is hard-coded in components — every component resolves Tailwind
 * utilities (bg-surface-2, text-accent, rounded-card …) to the CSS variables this file emits
 * (see applyTheme.ts + tailwind.config.js). The runtime can ALSO override any token from the store
 * (white-label brand colour, density) — see applyTheme(overrides).
 *
 * Light-first: `defaultAppearance` is 'light'; the user can still toggle to dark.
 */

export type Appearance = 'light' | 'dark';

/** A flat colour palette — keys become CSS custom properties (--surface-0, --accent, …). */
export interface Palette {
  'surface-0': string;
  'surface-1': string;
  'surface-2': string;
  'surface-3': string;
  'text-primary': string;
  'text-secondary': string;
  'text-muted': string;
  'border-subtle': string;
  'border-strong': string;
  accent: string;
  'accent-hover': string;
  'accent-contrast': string;
  'accent-soft': string;
  'accent-on-soft': string;
  success: string;
  'success-soft': string;
  'success-on-soft': string;
  warning: string;
  'warning-soft': string;
  'warning-on-soft': string;
  danger: string;
  'danger-soft': string;
  'danger-on-soft': string;
  'danger-contrast': string;
  info: string;
  'info-soft': string;
  'info-on-soft': string;
  /** GOLD — reserved for a gift: a standard someone may use free for a while. It is deliberately NOT
   *  `warning` (amber already means "expiring soon" on these same cards) and NOT `accent` (which means
   *  "act on this"). A colour that means one thing keeps meaning it. */
  gift: string;
  'gift-soft': string;
  'gift-on-soft': string;
  'gift-contrast': string;
  'focus-ring': string;
  /** Scrim behind modals/drawers/popovers. A pre-composited translucent ink so every overlay across the
   *  three apps uses ONE backdrop, theme-aware, instead of a hand-rolled `bg-black/50`. */
  overlay: string;
}

/** Non-colour design tokens — shared across appearances (also emitted as CSS vars). */
export interface Scale {
  /** Root font-size as a percentage of 16px; scales the WHOLE UI (rem-based). */
  rootFontPct: number;
  /** Corner radii. `card` is the signature large radius for panels. */
  radius: { sm: string; md: string; lg: string; card: string; pill: string };
  /** Elevation shadows (control-room: soft, low-spread). */
  shadow: { sm: string; md: string; lg: string; pop: string; glow: string };
  /** Motion durations + easing for the consistent, calm feel. */
  motion: { fast: string; base: string; slow: string; ease: string };
  /** Font families (Arabic-aware face swapped at dir=rtl in index.css). */
  font: { sans: string; mono: string };
}

// ── the accent: ONE source of truth ───────────────────────────────────────────────────
// The brand accent lives in config/product.json (brand.accent) and is injected here at build time as
// __PRODUCT_ACCENT__. To rebrand the whole product — every button, link, focus ring, glow, and the logo
// shield — you change ONE value in product.json. Nothing else. The full ramp (hover, soft tint, on-soft
// text) is DERIVED from that base below, so there is no second place to keep in sync.
//
// The fallback (royal blue) is only for a context where the define is absent — a test runner, Storybook.
declare const __PRODUCT_ACCENT__: { light: string; dark: string };
const BRAND = typeof __PRODUCT_ACCENT__ !== 'undefined'
  ? __PRODUCT_ACCENT__
  : { light: '#B8862B', dark: '#D8B876' };

/** Mix two hex colours by `amount` (0..1 toward `to`). Lets us derive a hover, a soft tint and on-soft
 *  text from a single brand hex, so the ramp is computed rather than hand-picked in a second place. */
function mix(from: string, to: string, amount: number): string {
  // The `#` is stripped first, so the channels start at 0/2/4 — NOT 1/3/5. With 1/3/5 (a leftover
  // from indexing WITH the '#'), h('ffffff') read ['ff','ff','f'] → blue=15 not 255, so every derived
  // tint skewed hard toward yellow (accent-soft rendered #f1ec0f, a highlighter, on the active nav row).
  const h = (c: string) => [0, 2, 4].map((i) => parseInt(c.slice(i, i + 2), 16));
  const [r1, g1, b1] = h(from.replace('#', ''));
  const [r2, g2, b2] = h(to.replace('#', ''));
  const m = (a: number, b: number) => Math.round(a + (b - a) * amount).toString(16).padStart(2, '0');
  return `#${m(r1, r2)}${m(g1, g2)}${m(b1, b2)}`;
}

// LIGHT: hover darkens toward ink; soft is a pale tint of the accent; on-soft reuses the hover for text.
const ACCENT_LIGHT = {
  base: BRAND.light,
  hover: mix(BRAND.light, '#000000', 0.22),
  soft: mix(BRAND.light, '#ffffff', 0.88),
  onSoft: mix(BRAND.light, '#000000', 0.22),
};
// DARK: the base is already lifted for the near-black ground; hover lightens, soft is a deep tint.
const ACCENT_DARK = {
  base: BRAND.dark,
  hover: mix(BRAND.dark, '#ffffff', 0.28),
  soft: mix(BRAND.dark, '#0a0e1a', 0.80),
  onSoft: mix(BRAND.dark, '#ffffff', 0.40),
};

const light: Palette = {
  // AIR, not grey. The old neutrals were a heavy grey-blue (#e8edf6 ground, #dde5f1/#ccd8ea fills,
  // #d3ddec borders) so everything that wasn't white read as grey and the life drained out. Now the
  // canvas is nearly white with only a whisper of brand tint, fills are near-white (used rarely), and
  // borders are faint hairlines — grey is demoted to lines and muted captions ONLY. White breathes,
  // cards lift on soft shadow, and colour is free to carry the meaning.
  // WARM PAPER, not cold air. The identity is a sealed, notarised document: a warm cream ground
  // (#F6F4EF), white cards that lift on a navy-tinted shadow, warm hairline borders, and DEEP NAVY ink.
  // Gold is the accent (authority), teal the secondary. This is the "premium legal paper" light theme.
  'surface-0': '#f6f4ef',
  'surface-1': '#ffffff',
  'surface-2': '#efeadd',
  'surface-3': '#e5dfce',
  'text-primary': '#151d33',
  'text-secondary': '#43506e',
  'text-muted': '#8a8472',
  'border-subtle': '#e6e0d0',
  'border-strong': '#d5cfba',
  accent: ACCENT_LIGHT.base,
  'accent-hover': ACCENT_LIGHT.hover,
  // Gold is a mid-tone: DARK INK on gold reads 7:1, white only ~2.3:1. So a gold button carries navy ink.
  'accent-contrast': '#151d33',
  'accent-soft': ACCENT_LIGHT.soft,
  'accent-on-soft': ACCENT_LIGHT.onSoft,
  success: '#0f7a54',
  'success-soft': '#dcefe4',
  'success-on-soft': '#0a5c3f',
  warning: '#b7791f',
  'warning-soft': '#f7ecd2',
  'warning-on-soft': '#8a5a12',
  danger: '#b3492f',
  'danger-soft': '#f5e2da',
  'danger-on-soft': '#8f3822',
  'danger-contrast': '#ffffff',
  // SECONDARY = teal (the sealed-green companion to the gold). Also used for the info role.
  info: '#0f766e',
  'info-soft': '#e2f2ef',
  'info-on-soft': '#0b5850',
  // The gift accent (a free standard) must stay DISTINCT from the gold accent — so it is teal here,
  // the calm secondary, never the gold that now carries the brand.
  gift: '#0f766e',
  'gift-soft': '#e2f2ef',
  'gift-on-soft': '#0b5850',
  'gift-contrast': '#ffffff',
  'focus-ring': ACCENT_LIGHT.base,
  overlay: 'rgb(21 29 51 / 0.45)',
};

const dark: Palette = {
  // "Night control room" — a rich near-black with a cool royal-blue undertone, not flat charcoal.
  // Surfaces step up in luminance with a hint of the brand hue so panels read as layered, and borders
  // are lifted a step so a card has a visible edge against the panel below it — the same depth fix as
  // the light theme, the mirror flatness gone on both grounds.
  // "Sealed at night" — deep NAVY grounds (not charcoal), warm cream text, gold accent, teal secondary.
  'surface-0': '#0b1120',
  'surface-1': '#12192b',
  'surface-2': '#1b2540',
  'surface-3': '#26314c',
  // HIGH CONTRAST on the deep-navy ground: cream primary, bright secondary, and a muted that is still
  // clearly legible (never a dim grey). Measured to stay ≥ 4.5:1 on surface-0/1.
  'text-primary': '#f4efe2',
  'text-secondary': '#dbe2f0',
  'text-muted': '#b3bed8',
  'border-subtle': '#2b3752',
  'border-strong': '#465274',
  accent: ACCENT_DARK.base,
  'accent-hover': ACCENT_DARK.hover,
  // Gold on navy also takes dark ink for a filled button.
  'accent-contrast': '#12192b',
  'accent-soft': ACCENT_DARK.soft,
  'accent-on-soft': ACCENT_DARK.onSoft,
  success: '#34d399',
  'success-soft': '#0c2f26',
  'success-on-soft': '#6ee7b7',
  warning: '#e8c469',
  'warning-soft': '#3a2d0e',
  'warning-on-soft': '#f5d98d',
  danger: '#f0836a',
  'danger-soft': '#3a1a12',
  'danger-on-soft': '#f7b3a1',
  'danger-contrast': '#0b1120',
  info: '#5eccc0',
  'info-soft': '#0c322e',
  'info-on-soft': '#9be3d9',
  // Gift stays teal (distinct from the gold brand accent) on the navy ground.
  gift: '#5eccc0',
  'gift-soft': '#0c322e',
  'gift-on-soft': '#9be3d9',
  'gift-contrast': '#0b1120',
  'focus-ring': ACCENT_DARK.hover,
  overlay: 'rgb(5 8 16 / 0.62)',
};

const scale: Scale = {
  // 100% (=16px). 112.5% read oversized — the whole UI at ~90% browser zoom was the comfortable size,
  // i.e. 112.5%×0.9≈101%. Sizing the root here means every rem-based token shrinks together, so the
  // sidebar fits without a scroll and reading is easier, with no per-component change.
  rootFontPct: 100,
  // A touch more generous — a larger card radius reads as premium, not utilitarian.
  radius: { sm: '0.5rem', md: '0.625rem', lg: '0.875rem', card: '1.25rem', pill: '9999px' },
  // Layered elevation, SOFTENED. A tight contact shadow plus a large diffuse one at low opacity, so a
  // panel lifts off the ground without a heavy grey halo — the airy premium feel comes from big blur at
  // low opacity, not from a darker shadow. `glow` is the accent-tinted hover lift, the gift-card glow
  // generalised: any interactive card can wear it on hover and it picks up whatever the accent is.
  // Softly tinted toward the brand (royal-blue ink, not neutral black) so white cards lift off the airy
  // near-white canvas without a heavy grey halo — the premium feel comes from big blur at low opacity.
  // Tinted toward the NAVY ink (not neutral black) so white cards lift off the warm cream canvas with a
  // soft, premium halo — big blur at low opacity, never a heavy grey shadow.
  shadow: {
    sm: '0 1px 2px -1px rgb(21 29 51 / 0.10), 0 1px 1px rgb(21 29 51 / 0.05)',
    md: '0 2px 8px -3px rgb(21 29 51 / 0.12), 0 10px 26px -8px rgb(21 29 51 / 0.14)',
    lg: '0 8px 18px -8px rgb(21 29 51 / 0.14), 0 24px 52px -16px rgb(21 29 51 / 0.20)',
    pop: '0 12px 26px -12px rgb(21 29 51 / 0.18), 0 34px 72px -20px rgb(21 29 51 / 0.28)',
    glow: '0 10px 34px -8px color-mix(in srgb, var(--accent) 42%, transparent)',
  },
  motion: { fast: '120ms', base: '200ms', slow: '360ms', ease: 'cubic-bezier(0.22, 1, 0.36, 1)' },
  font: {
    // ONE central stack for the whole product — the premium identity face. IBM Plex Sans Arabic carries
    // BOTH Arabic and Latin with a formal, engineered feel (the "sealed document" voice); Inter is the
    // Latin fallback. To change it everywhere, swap the first family here AND the webfont @import in each
    // app's index.css — nothing else.
    sans: "'IBM Plex Sans Arabic', 'Inter', ui-sans-serif, system-ui, 'Segoe UI', Tahoma, Arial, sans-serif",
    mono: "'JetBrains Mono', ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace",
  },
};

export const themeConfig = {
  defaultAppearance: 'light' as Appearance,
  palettes: { light, dark } as Record<Appearance, Palette>,
  scale,
};

export type ThemeConfig = typeof themeConfig;
