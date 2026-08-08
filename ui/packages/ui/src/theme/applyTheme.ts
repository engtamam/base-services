/**
 * applyTheme — emit the theme.config tokens as CSS custom properties onto <html>, so a single config
 * edit (or a store/white-label override) restyles the WHOLE app with zero per-component changes.
 *
 * `overrides` lets the backend settings drive the look (e.g. brand.accent) — values come from the
 * store, never hard-coded in the component tree. Unknown override keys are ignored (safe).
 */
import { themeConfig, type Appearance, type Palette } from './theme.config';

const PALETTE_KEYS = Object.keys(themeConfig.palettes.light) as (keyof Palette)[];

/** Mix two hex colours by `amount` (0..1 toward `to`) — mirrors theme.config's ramp math so a
 *  white-label accent derives the SAME way the built-in palette does. */
function mix(from: string, to: string, amount: number): string {
  const h = (c: string) => [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));
  const norm = (c: string) => { const v = c.replace('#', ''); return v.length === 3 ? v.split('').map((x) => x + x).join('') : v; };
  const [r1, g1, b1] = h(`#${norm(from)}`);
  const [r2, g2, b2] = h(`#${norm(to)}`);
  const m = (a: number, b: number) => Math.round(a + (b - a) * amount).toString(16).padStart(2, '0');
  return `#${m(r1, r2)}${m(g1, g2)}${m(b1, b2)}`;
}

/** Perceived brightness of a hex colour, 0 (black) … 1 (white). Used to pick a text/icon colour that
 *  contrasts with a tint whatever its hue — light tint → dark ink, dark tint → light ink. */
function relLuminance(hex: string): number {
  const v = hex.replace('#', '');
  const f = v.length === 3 ? v.split('').map((x) => x + x).join('') : v;
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(f.slice(i, i + 2), 16) / 255);
  return 0.299 * r + 0.587 * g + 0.114 * b;
}

/** Map a small set of store keys → palette tokens they may override (white-label brand colour).
 *
 *  A white-label accent must drive the WHOLE accent ramp, not just the base. Setting only `--accent`
 *  (as this used to) left `--accent-soft`/`--accent-on-soft` at the built-in hue, so a brand colour
 *  rendered as a bright fill with low-contrast text on the soft badges and the active nav item. We now
 *  re-derive soft/hover/on-soft from the override the same way theme.config does — appearance-aware, so
 *  the soft is a genuinely dark tint in dark mode (light in light mode) and the on-soft text always has
 *  a strong luminance gap against it, whatever hue the brand picks. */
function paletteOverrides(overrides: Record<string, string | undefined>, appearance: Appearance): Partial<Palette> {
  const out: Partial<Palette> = {};
  const accent = overrides['brand.accent'] || overrides['brand.color'];
  if (accent && /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(accent.trim())) {
    const a = accent.trim();
    out.accent = a;
    out['focus-ring'] = a;
    if (appearance === 'dark') {
      out['accent-hover'] = mix(a, '#ffffff', 0.28);
      out['accent-soft'] = mix(a, '#0a0e1a', 0.82);   // deep tint on the near-black ground
      out['accent-on-soft'] = mix(a, '#ffffff', 0.62);  // lifted well clear of the soft for contrast
    } else {
      out['accent-hover'] = mix(a, '#000000', 0.22);
      out['accent-soft'] = mix(a, '#ffffff', 0.88);   // pale tint
      out['accent-on-soft'] = mix(a, '#000000', 0.30);  // dark text on the pale tint
    }
  }
  return out;
}

export function applyTheme(
  appearance: Appearance,
  overrides: Record<string, string | undefined> = {},
): void {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  const palette: Palette = { ...themeConfig.palettes[appearance], ...paletteOverrides(overrides, appearance) };

  // KEEP the soft tint (its colour is a deliberate design choice — e.g. the lime highlight), but ALWAYS
  // recompute the TEXT/ICON colour on it for contrast. The bug was never the soft's hue; it was that
  // `--accent-on-soft` sat at nearly the same luminance as `--accent-soft` (lime on lime → invisible).
  // We derive on-soft from the soft's own luminance: dark ink on a light tint, light ink on a dark one —
  // so labels and icons read clearly whatever the soft colour is.
  const soft = (palette['accent-soft'] || '').trim();
  if (/^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(soft)) {
    palette['accent-on-soft'] = relLuminance(soft) > 0.5
      ? mix(soft, '#0a0f0a', 0.80)   // dark ink, tinted by the soft's own hue
      : mix(soft, '#ffffff', 0.72);  // light ink for a dark tint
  }
  for (const key of PALETTE_KEYS) root.style.setProperty(`--${key}`, palette[key]);

  const s = themeConfig.scale;
  root.style.setProperty('--radius-sm', s.radius.sm);
  root.style.setProperty('--radius-md', s.radius.md);
  root.style.setProperty('--radius-lg', s.radius.lg);
  root.style.setProperty('--radius-card', s.radius.card);
  root.style.setProperty('--radius-pill', s.radius.pill);
  root.style.setProperty('--shadow-sm', s.shadow.sm);
  root.style.setProperty('--shadow-md', s.shadow.md);
  root.style.setProperty('--shadow-lg', s.shadow.lg);
  root.style.setProperty('--shadow-pop', s.shadow.pop);
  root.style.setProperty('--shadow-glow', s.shadow.glow);
  root.style.setProperty('--motion-fast', s.motion.fast);
  root.style.setProperty('--motion-base', s.motion.base);
  root.style.setProperty('--motion-slow', s.motion.slow);
  root.style.setProperty('--motion-ease', s.motion.ease);
  root.style.setProperty('--font-sans', s.font.sans);
  root.style.setProperty('--font-mono', s.font.mono);
  root.style.setProperty('--root-font-pct', `${s.rootFontPct}%`);
  // Focus-ring offset — the gap between a control's edge and its focus outline, so the ring reads on
  // any surface. A constant here (not per-appearance) but tokenised so a control on a tight/solid fill
  // can pull it inward via the `.focus-ring-inset` utility instead of hard-coding an offset.
  root.style.setProperty('--focus-ring-offset', '2px');

  root.setAttribute('data-theme', appearance);
}
