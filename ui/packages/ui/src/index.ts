// @base-ui/source — the shared UI infrastructure barrel.
// Prefer the subpath entries (@base-ui/theme, @base-ui/design-system, …) in app code; this barrel
// is the convenience surface. Every export here is token-only and app-agnostic.
export * from './theme';
export { cn } from './lib/cn';
