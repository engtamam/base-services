// Unified editor — Phase 1 (#892): bridge the LEGAL block model into the ONE compliance renderer.
//
// Two block families exist today (see DocBlockRenderer's header): the compliance model `{ kind: {…} }`
// with nested-bilingual `{en,ar}` fields (the 17 signed verbs), and the older legal model `{ type, data }`
// with flat markdown bodies (`md_en`/`md_ar`) rendered by the separate BlockRenderer. We are converging on
// ONE read renderer (the compliance `DocBlocksView`). This converter is the safe seam:
//
//   • A section TITLE becomes a real compliance `section` block — it renders today, no new renderer needed.
//   • Every legacy legal block is carried through under a NAMESPACED kind (`legal_text`, `legal_callout`,
//     `legal_table`, `legal_image`, `legal_divider`) so it can NEVER collide with a compliance kind of the
//     same bare name (a legal `callout` is flat markdown; a compliance `callout` is structured items — the
//     exact clash that kept the two renderers apart). The renderer gains one entry per `legal_*` kind.
//
// This is additive and lossless: the legal `data` payload is passed through untouched, so nothing about a
// published legal document changes on the wire, and no data migration is required to render it through the
// unified renderer. New legal authoring will emit the rich compliance kinds directly; legacy documents keep
// rendering via these `legal_*` shims until (optionally) migrated in Phase 2.
import type { DocBlock } from './DocBlockRenderer';

// Localized so the shared module imports no app file. Mirrors the client's legal.ts Section/Block shape.
type LegalBlock = { type: string; data: Record<string, unknown> };
type LegalSection = { title_ar: string; title_en: string; blocks: LegalBlock[] };

/** The legacy legal kinds, namespaced so they never collide with a compliance kind. Kept in one place so
 *  the renderer and this converter cannot drift. */
export const LEGAL_KIND_PREFIX = 'legal_';
export const LEGAL_LEGACY_KINDS = ['text', 'callout', 'table', 'image', 'divider'] as const;
export type LegalLegacyKind = (typeof LEGAL_LEGACY_KINDS)[number];

/** `legal_text` → `text`, etc. Returns null for a kind that is NOT a legal-legacy shim. */
export function legalKindBase(kind: string): LegalLegacyKind | null {
  if (!kind.startsWith(LEGAL_KIND_PREFIX)) return null;
  const base = kind.slice(LEGAL_KIND_PREFIX.length);
  return (LEGAL_LEGACY_KINDS as readonly string[]).includes(base) ? (base as LegalLegacyKind) : null;
}

/** One legal `{ type, data }` block → one `{ [kind]: data }` block for the unified renderer.
 *  · a LEGACY legal kind (text/callout/table/image/divider — flat markdown) is namespaced `legal_*` so the
 *    renderer delegates it to the legal (markdown) renderer;
 *  · a COMPLIANCE kind (clause/section/lead/…) passes THROUGH unchanged so the compliance renderer renders
 *    it directly — this is what lets the operator author legal docs in the rich 17-kind vocabulary.
 *  The `data` payload is passed through verbatim either way. */
export function legalBlockToDocBlock(b: LegalBlock): DocBlock {
  const isLegacy = (LEGAL_LEGACY_KINDS as readonly string[]).includes(b.type);
  const kind = isLegacy ? `${LEGAL_KIND_PREFIX}${b.type}` : b.type;
  return { [kind]: b.data } as DocBlock;
}

/** A whole legal document's sections → a flat `DocBlock[]` the unified `DocBlocksView` can render:
 *  each non-empty section title becomes a real compliance `section` block, followed by its (namespaced)
 *  legacy blocks. Titles carry both editions so the renderer resolves per the shown edition. */
export function legalSectionsToDocBlocks(sections: LegalSection[]): DocBlock[] {
  const out: DocBlock[] = [];
  for (const s of sections) {
    const hasTitle = (s.title_en && s.title_en.trim()) || (s.title_ar && s.title_ar.trim());
    if (hasTitle) {
      out.push({ section: { level: 2, text: { en: s.title_en ?? '', ar: s.title_ar ?? '' } } } as DocBlock);
    }
    for (const b of s.blocks ?? []) out.push(legalBlockToDocBlock(b));
  }
  return out;
}
