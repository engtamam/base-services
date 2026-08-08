// Shared site-content surface: the model, the static fallback, and the one public footer renderer used
// by both the client app and the operator editor's live preview.
export * from './types';
export * from './fallback';
export { SiteFooter, bannerVisible, type SiteFooterProps } from './SiteFooter';
