// The paper/block document engine.
//
// DocBlockRenderer is the on-page, Word-like editor: it renders a document as typed blocks (cover,
// section, lead, clause, table, callout, …) and — when the host passes edit ops — grows drag handles,
// delete controls and a "+" palette to insert sections ON THE SAME PAGE, plus inline table and prose
// editing. There is NO separate editor that reflows the paper into a second page. `{{TOKEN}}`
// placeholders fill live from the tenant's saved answers; content is always rendered as TEXT (never
// innerHTML). The block VOCABULARY is data the host supplies, so a new block kind is config, not code.
//
// BlockRenderer is the sibling `{ type, data }` model; both share inlineMarks so prose formats
// identically. blockdoc is the section-rail workspace (SectionRail + SectionPane + BlockDocEditor) for
// editing a long document a section at a time, with example vocabularies a host can start from.

// ── on-page renderer (kind model: `{ kind: {…} }`) ──────────────────────────────────────────────────
export { DocBlockView, DocBlocksView, KNOWN_DOC_BLOCK_KINDS } from './DocBlockRenderer';
export type { EditOps, BlockChrome, DocBlock, ResolveFn } from './DocBlockRenderer';

// ── {type,data} model renderer ──────────────────────────────────────────────────────────────────────
export { BlockView, KNOWN_BLOCK_TYPES } from './BlockRenderer';

// ── the section-workspace editor + its vocabulary (Block/Section/Draft are defined here) ─────────────
export {
  BlockDocEditor, BlockWorkspace, SectionRail, SectionPane, BlockIndexRail,
  BlockDocI18nProvider, useBlockDocI18n, blockKindLabel,
  kindToBlock, blockToKind, flatSection, blockPreview, AutoTextarea,
  BLOCK_FIELDS, ADDABLE, LEGAL_VOCAB, COMPLIANCE_VOCAB, COMPLIANCE_BLOCK_FIELDS,
  COMPLIANCE_ADDABLE, FIELD,
} from './blockdoc';
export type {
  Block, Section, Draft, FieldDef, Vocabulary, Edition, Envelope, KindBlock, BlockDocI18n,
} from './blockdoc';

export { inlineMarks } from './inlineMarks';
