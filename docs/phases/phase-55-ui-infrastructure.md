# Phase 55 — `ui/`: a premium, themeable component infrastructure (adopted from Kobo)

The 23 services expose everything a UI needs (manifests, admin/user actions, OpenAPI). This phase
gives adopters the **front-end infrastructure** to build on: a React + Tailwind component library
that is premium, fully themeable, RTL/Arabic-ready, and — the rule that makes it reusable —
**carries no hard-coded colour, string, or API**. It is lifted from a mature product library
(`Kobo/ui/packages/imtithalak-ui`) and generalised: the design system and the paper/block editor come
across intact; the app-specific wiring is replaced by props/seams so any host can bind it.

## The belief this phase is built to destroy

> "A component library ships buttons. A theme is a colour you set in CSS. The editor is a rich-text
> box, and the login page is glued to one backend."

Every clause is what makes a UI un-reusable. This one is the opposite:

- **Nothing is hard-coded.** Every colour is a token (`bg-accent`, `text-text-primary`) resolved to a
  CSS variable emitted from ONE `theme.config.ts`; a white-label brand colour is a runtime override,
  not a component change. Every string comes through an i18n seam (`{ t, locale }`), en/ar with RTL.
- **The paper IS the editor.** The block renderer edits the document *in place* — drag/insert/delete
  sections and edit tables and prose on the same page, Word-like — never a separate editor that
  reflows the paper into "a second page". `{{tokens}}` fill live from saved answers.
- **The library holds no API.** Data and callbacks are props; a host app owns loading/saving/auth.

## What we adopt (from Kobo/ui/packages/imtithalak-ui/src)

- **`theme/`** — `theme.config.ts` (light+dark palettes, radii, shadows, type scale, motion) +
  `applyTheme.ts` (emits CSS vars on `<html>`, derives a full accent ramp from a brand override with
  contrast math) + a shareable Tailwind preset mapping utilities → vars.
- **`lib/cn.ts`** — clsx + tailwind-merge.
- **`design-system/`** — ~23 primitives (Button/Card/Modal/Drawer/Tabs/Toast/Field/KpiCard/Segmented/
  Badge/StatusChip/Skeleton/EmptyState/…), all cva-variant, token-only.
- **`components/` (the paper/block engine)** — `DocBlockRenderer` (on-page Word-like editing:
  drag/insert/delete blocks, inline table + prose edit, live `{{token}}` fill), `BlockRenderer`
  (`{type,data}` model), `inlineMarks`, `blockdoc` workspace. Block vocabulary is DATA (a `BLOCK_SPEC`
  from the backend), so a new block kind is config, not code.
- **`site/SiteFooter`** — the one config-driven footer (socials, banner window, order/hidden, i18n).
- **`i18n/`** — the catalog + locale/RTL helpers.

## What we build generic (decoupled from the product)

Shared pages every app needs, presentational and prop-driven (the host wires the API):

- **`auth/AuthLayout`** — the split brand/pane shell.
- **`auth/LoginForm`** — email + password, org-picker step; no "organization" field (the backend
  decides which orgs a person may enter). Emits `onLogin` / `onChooseOrg`; renders error CODES via the
  i18n seam, never raw keys.
- **`auth/SignupForm`** — company/individual signup; emits `onSignup`.
- **`site/Footer`** wired form + `AppShell` header/nav slots.

## The work

### 55.1 — scaffold `ui/` + the theme engine + `cn`
- `ui/packages/ui` as a source-consumed package (`@base-ui/source`): package.json (React/tailwind-merge/
  clsx/cva/lucide/react-router as PEERS), tsconfig, a shareable `tailwind-preset`. Copy `theme/` +
  `lib/cn` verbatim, rename the alias. A tiny demo app or Vitest proves it mounts + themes.

### 55.2 — the design system (~23 primitives)
- Copy `design-system/*`, token-only, cva variants; barrel `index.ts`. No app imports.

### 55.3 — the paper/block editor
- Copy `BlockRenderer` + `DocBlockRenderer` + `inlineMarks` + `blockdoc`; the block vocabulary is a
  prop (`BLOCK_SPEC`), the i18n a seam. On-page editing (move/insert/delete/edit) via injected ops.

### 55.4 — shared pages (login / signup / footer / shell)
- Generic `AuthLayout` + `LoginForm` + `SignupForm` + `SiteFooter`(+wired) + `AppShell`. No backend
  import — props and callbacks only, so a host binds them to identity-service / any API.

### 55.5 — i18n + RTL + theming polish
- The i18n seam + en/ar catalog scaffold; `[dir='rtl']` correctness; light/dark toggle; a brand
  override demo (white-label accent).

### 55.6 — a demo/storybook app + tests
- A tiny Vite demo mounting the primitives, the paper editor, and the auth pages against a fake API,
  so an adopter sees the whole kit and copies from a working example. Component tests (Vitest/RTL).

## Definition of Done

- `ui/packages/ui` is source-consumable via a Vite/tsconfig alias, peers supplied by the host.
- Zero hard-coded colours/strings/API in the library; every colour is a token, every string an i18n
  key, every data/callback a prop. A white-label brand colour is one runtime override.
- The paper/block editor edits in place (no second page): move/insert/delete sections, edit tables and
  prose on the same paper, live `{{token}}` fill; block vocabulary is data.
- Generic auth pages (login/signup), a config-driven footer, and an app shell — all prop-driven.
- en/ar + RTL correct; light/dark; a demo app mounts the whole kit; component tests green.
- Permissive licences only (react, lucide-react, clsx, tailwind-merge, class-variance-authority,
  react-router — all MIT/ISC).

## Tasks

- [x] **55.1** Scaffold `ui/packages/ui` (`@base-ui/source`, peers, tsconfig, tailwind-preset) + copy
  the `theme/` engine + `lib/cn`; prove it mounts + themes.
- [x] **55.2** Copy the design system (~23 cva primitives), token-only, barrel export.
- [x] **55.3** Copy the paper/block editor (`BlockRenderer`, `DocBlockRenderer`, `inlineMarks`,
  `blockdoc`); vocabulary + i18n as seams; on-page editing.
- [x] **55.4** Generic shared pages: `AuthLayout`, `LoginForm`, `SignupForm`, `SiteFooter`(+wired),
  `AppShell` — prop-driven, no backend import.
- [x] **55.5** i18n seam + en/ar catalog + RTL + light/dark toggle + white-label brand override demo.
- [x] **55.6** Demo app (Vite) + component tests (Vitest/RTL) mounting primitives, paper editor, auth.


---

**Phase 55 complete.** A premium, fully-themeable, RTL/Arabic-ready React+Tailwind UI infrastructure under `ui/` (adopted+generalised from Kobo): the theming engine (one theme.config -> CSS vars, white-label brand override, light/dark), 23 cva design-system primitives, the on-page Word-like paper/block editor (edit sections on the same paper, live {{token}} fill — the replacement for the free editor that split the page), generic auth (login/signup) + config-driven footer + app shell, and an en/ar i18n seam with RTL. Nothing is hard-coded: every colour is a token, every string an i18n key, every data/callback a prop. Consumed AS SOURCE via the @base-ui alias. A demo app mounts the whole kit; `tsc --noEmit` typechecks clean and a vitest test passes — the entire library is verified to compile.
