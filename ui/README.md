# base-services UI infrastructure

A premium, **fully-themeable**, RTL/Arabic-ready React + Tailwind component library — plus a
paper/block document editor and generic auth/footer pages. Built so an adopter can build their
product UI fast and wire it to the 23 services.

## The rule: nothing is hard-coded
- **Colours** are semantic tokens (`bg-accent`, `text-text-primary`) resolved to CSS variables
  emitted from ONE `theme.config.ts` (`@base-ui/theme`). A white-label brand colour is a *runtime*
  override (`applyTheme({ 'brand.accent': '#0F5F4E' })`), never a component change.
- **Strings** come through an i18n seam (`{ t, locale }`), en/ar with `[dir='rtl']`.
- **Data & API** are props/callbacks — the library imports no backend.

## Consume it (as source, no npm install)
Each app aliases `@base-ui` to `packages/ui/src` (Vite + tsconfig `paths`) and supplies the peers
(react, react-dom, react-router-dom, lucide-react, clsx, tailwind-merge, class-variance-authority).
Tailwind: `presets: [require('@base-ui/source/tailwind-preset.cjs')]` and set your own `content`.

## Layout
- `packages/ui/src/theme` — the theming engine (config → CSS vars, brand overrides, light/dark)
- `packages/ui/src/lib/cn.ts` — clsx + tailwind-merge
- `packages/ui/src/design-system` — the primitives (Button, Card, Modal, … ; cva, token-only)
- `packages/ui/src/components` — the paper/block editor (on-page, Word-like)
- `packages/ui/src/site` — the config-driven footer
- `packages/ui/src/auth` — generic login/signup/shell (prop-driven)
- `apps/demo` — a working example that mounts the whole kit

Adopted and generalised from a mature product library; permissive deps only (MIT/ISC).
