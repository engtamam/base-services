/**
 * @base-ui Tailwind preset — semantic tokens -> CSS vars (color-mix oklab for alpha).
 * Shareable: an app does `presets: [require('@base-ui/source/tailwind-preset.cjs')]` and
 * sets its own `content`. The var palettes are emitted by theme/applyTheme.ts from theme.config.ts,
 * so ONE config restyles everything and a brand override is a runtime value, never a class.
 * Adopted from Kobo's proven config. Permissive (Tailwind v3, MIT).
 */
// Tailwind v3 config (PostCSS pipeline — no lightningcss/MPL, no v4 @tailwindcss/postcss). Utilities
// resolve to semantic CSS vars, and the vars are swapped per appearance via [data-theme] on <html>
// (see src/index.css for the var palettes). Adapted from the Owner Hub's proven config.
//
// Opacity modifiers (e.g. `bg-success-soft`, `border-danger/40`) are supported via the function color
// form below — we emit `color-mix(in oklab, …)` so alpha on var-backed colors renders correctly.
const v = (name) =>
  ({ opacityValue }) =>
    opacityValue === undefined
      ? `var(${name})`
      : `color-mix(in oklab, var(${name}) calc(${opacityValue} * 100%), transparent)`;

module.exports = {
  // Include the shared UI library source so its utility classes (now living outside this app) are
  // scanned and NOT purged — this is what keeps the on-prem output byte-identical after the U1 move.
  theme: {
    extend: {
      colors: {
        surface: {
          0: v('--surface-0'),
          1: v('--surface-1'),
          2: v('--surface-2'),
          3: v('--surface-3'),
        },
        text: {
          primary: v('--text-primary'),
          secondary: v('--text-secondary'),
          muted: v('--text-muted'),
        },
        border: {
          subtle: v('--border-subtle'),
          strong: v('--border-strong'),
        },
        accent: {
          DEFAULT: v('--accent'),
          hover: v('--accent-hover'),
          contrast: v('--accent-contrast'),
          soft: v('--accent-soft'),
          'on-soft': v('--accent-on-soft'),
        },
        success: {
          DEFAULT: v('--success'),
          soft: v('--success-soft'),
          'on-soft': v('--success-on-soft'),
        },
        gift: {
          DEFAULT: v('--gift'),
          soft: v('--gift-soft'),
          'on-soft': v('--gift-on-soft'),
          contrast: v('--gift-contrast'),
        },
        warning: {
          DEFAULT: v('--warning'),
          soft: v('--warning-soft'),
          'on-soft': v('--warning-on-soft'),
        },
        danger: {
          DEFAULT: v('--danger'),
          soft: v('--danger-soft'),
          'on-soft': v('--danger-on-soft'),
          contrast: v('--danger-contrast'),
        },
        info: {
          DEFAULT: v('--info'),
          soft: v('--info-soft'),
          'on-soft': v('--info-on-soft'),
        },
        // Scrim behind modals/drawers/popovers — one theme-aware backdrop token (see --overlay).
        overlay: v('--overlay'),
      },
      // NAMED TYPE SCALE (additive). Each step pairs a size with a line-height and letter-spacing so
      // `text-h1`, `text-body`, `text-2xs` … carry their rhythm with them — no separate leading/tracking
      // utility to remember. Tailwind's built-in sizes are untouched; apps adopt these incrementally.
      // The `2xs`/`3xs` steps also back the existing `text-2xs`/`text-3xs` usages across the three apps.
      fontSize: {
        display: ['2rem', { lineHeight: '2.5rem', letterSpacing: '-0.02em' }],
        h1: ['1.5rem', { lineHeight: '2rem', letterSpacing: '-0.02em' }],
        h2: ['1.25rem', { lineHeight: '1.75rem', letterSpacing: '-0.01em' }],
        title: ['1.125rem', { lineHeight: '1.5rem', letterSpacing: '-0.01em' }],
        body: ['0.9375rem', { lineHeight: '1.5rem', letterSpacing: '0' }],
        label: ['0.8125rem', { lineHeight: '1.25rem', letterSpacing: '0' }],
        caption: ['0.75rem', { lineHeight: '1rem', letterSpacing: '0' }],
        overline: ['0.6875rem', { lineHeight: '1rem', letterSpacing: '0.08em' }],
        '2xs': ['0.625rem', { lineHeight: '0.875rem', letterSpacing: '0.01em' }],
        '3xs': ['0.5rem', { lineHeight: '0.75rem', letterSpacing: '0.02em' }],
      },
      // Radii / shadows / fonts / motion all resolve to the theme-config vars (single source of truth).
      borderRadius: {
        sm: 'var(--radius-sm)',
        md: 'var(--radius-md)',
        lg: 'var(--radius-lg)',
        card: 'var(--radius-card)',
        pill: 'var(--radius-pill)',
      },
      boxShadow: {
        sm: 'var(--shadow-sm)',
        md: 'var(--shadow-md)',
        lg: 'var(--shadow-lg)',
        pop: 'var(--shadow-pop)',
        glow: 'var(--shadow-glow)',
      },
      fontFamily: {
        sans: 'var(--font-sans)',
        mono: 'var(--font-mono)',
      },
      transitionTimingFunction: {
        smooth: 'var(--motion-ease)',
      },
      transitionDuration: {
        fast: 'var(--motion-fast)',
        base: 'var(--motion-base)',
        slow: 'var(--motion-slow)',
      },
    },
  },
  plugins: [
    // Custom focus outline using the --focus-ring var.
    function focusRing({ addUtilities }) {
      addUtilities({
        '.focus-ring:focus-visible': {
          outline: '2px solid var(--focus-ring)',
          'outline-offset': 'var(--focus-ring-offset, 2px)',
        },
        // For a focus target sitting flush against another surface (a segmented cell, a tight fill)
        // where an outward ring would be clipped or collide — the ring reads INSIDE the control.
        '.focus-ring-inset:focus-visible': {
          outline: '2px solid var(--focus-ring)',
          'outline-offset': '-2px',
        },
      });
    },
  ],
};
