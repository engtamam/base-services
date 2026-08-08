import {
  createContext, Fragment, useContext, useEffect, useLayoutEffect, useRef, useState, type ReactNode,
} from 'react';
import { ChevronUp, ChevronDown, ChevronRight, GripVertical, Plus, Trash2 } from 'lucide-react';
import { Badge } from '../design-system/Badge';
import { Button } from '../design-system/Button';
import { Segmented } from '../design-system/Segmented';

/**
 * THE SHARED BLOCK-DOCUMENT WORKSPACE (#605).
 *
 * Extracted verbatim from LegalPage's editor so ONE block editor serves both the legal documents (Terms,
 * Privacy, DPA) and the compliance policy templates authored in Catalog Studio. A document is a list of
 * SECTIONS, each an ordered list of typed BLOCKS; the workspace is a section rail beside a one-section
 * pane, so a long document is edited a section at a time. The block `type` is OPEN — friendly fields for
 * the kinds we know (BLOCK_FIELDS), a JSON escape hatch for the rest — so new block kinds cost no code.
 *
 * This module is pure UI: it holds no API. A wrapper (LegalPage's modal, the Studio policy editor) owns
 * loading, saving and publishing, and drives this with a `Draft` + an onChange.
 *
 * i18n SEAM (SPA-unification): the component reads `{ t, locale }` from a package-local context, fed by
 * each channel's own `useShell()` at the host boundary — it imports NO app, so both the admin and the
 * client can host it. See `BlockDocI18nProvider`.
 */

// ── the i18n seam ───────────────────────────────────────────────────────────────────────────────────
// A lightweight context so the shared editor never imports `@/app/Shell`. `t` is typed loosely so a
// channel's own strongly-typed dict function is assignable. Outside a provider it degrades to identity
// (the key itself) + English — the same fallback the design-system seam uses.
export interface BlockDocI18n { t: (key: string) => string; locale: 'en' | 'ar' }
const BlockDocI18nContext = createContext<BlockDocI18n>({ t: (k) => k, locale: 'en' });
export function BlockDocI18nProvider({ value, children }: { value: BlockDocI18n; children: ReactNode }) {
  return <BlockDocI18nContext.Provider value={value}>{children}</BlockDocI18nContext.Provider>;
}
export function useBlockDocI18n(): BlockDocI18n { return useContext(BlockDocI18nContext); }

/** A friendly, localised name for a block kind (so a customer sees «قائمة نقاط», not `bullets`). Falls
 *  back to the raw kind when a channel hasn't localised it — new kinds never break the editor. */
export function blockKindLabel(t: (k: string) => string, type: string): string {
  const key = `studio.block.kind.${type}`;
  const label = t(key);
  return label && label !== key ? label : type;
}

export interface Block { type: string; data: Record<string, unknown> }
export interface Section { title_ar: string; title_en: string; blocks: Block[] }
export interface Draft { title_ar: string; title_en: string; sections: Section[] }

// A field editor. `bilingual` means the key holds a nested `{ en, ar }` Text object (the compliance
// convention) rather than a flat string (the legal `md_en`/`md_ar` convention) — the two document
// families serialise bilingual text differently, so the editor supports both.
export type FieldDef = {
  key: string; labelKey: string; multiline?: boolean; select?: string[]; bilingual?: boolean;
  // STRUCTURED editors (compliance blocks) — so a customer never edits raw JSON. Each operates on the
  // block's array value with the SAME per-edition bilingual inputs the scalar fields use:
  //   listText     — an ordered list of bilingual Text (callout items, clause paras, signoff roles)
  //   listPair     — an ordered list of [Text, Text] rows (bullets, snapshot, fields, references);
  //                  `pair` names the two column label keys
  //   table        — a grid of bilingual Text cells (table rows, evidence rows)
  //   metricTable  — rows of { label, value, unit } bilingual Text (metric_table)
  kind?: 'listText' | 'listPair' | 'table' | 'metricTable';
  pair?: [string, string];
};

// A vocabulary is a family of block kinds: which get a friendly field editor (`fields`), and which can
// be inserted (`addable`). Anything addable but absent from `fields` still edits — as raw JSON. Two
// families exist, and they DELIBERATELY share kind NAMES with different shapes (a legal `callout` is
// flat markdown + tone; a compliance `callout` is a bilingual title + items + kind), so one global map
// cannot serve both — the editor takes the family as a prop instead.
export type Vocabulary = { fields: Record<string, FieldDef[]>; addable: string[] };

// The languages a bilingual field offers. Admin authors both; a channel with a single-edition tenant can
// narrow this (Phase 2) so a client sees only the editions it produces.
export type Edition = 'en' | 'ar';
const ALL_EDITIONS: Edition[] = ['ar', 'en'];   // #845 — Arabic first (home market is SA)

// ── the LEGAL family (Terms, Privacy, DPA) — the original set, unchanged ─────────────────────────
export const BLOCK_FIELDS: Record<string, FieldDef[]> = {
  text: [
    { key: 'md_en', labelKey: 'legal.f.bodyEn', multiline: true },
    { key: 'md_ar', labelKey: 'legal.f.bodyAr', multiline: true },
  ],
  callout: [
    { key: 'tone', labelKey: 'legal.f.tone', select: ['info', 'warn', 'success', 'danger'] },
    { key: 'md_en', labelKey: 'legal.f.bodyEn', multiline: true },
    { key: 'md_ar', labelKey: 'legal.f.bodyAr', multiline: true },
  ],
  image: [
    { key: 'ref', labelKey: 'legal.f.imageRef' },
    { key: 'alt', labelKey: 'legal.f.alt' },
    { key: 'caption_en', labelKey: 'legal.f.captionEn' },
    { key: 'caption_ar', labelKey: 'legal.f.captionAr' },
  ],
  divider: [],
};
export const ADDABLE = ['text', 'callout', 'table', 'image', 'divider'];
export const LEGAL_VOCAB: Vocabulary = { fields: BLOCK_FIELDS, addable: ADDABLE };

// ── the COMPLIANCE family (#606) — the policy-template blocks (services/documents BLOCK_SPEC) ─────
// Friendly editors cover the kinds whose args are all flat or bilingual Text; the structured kinds
// (bullets, clause, table, evidence, snapshot, fields, references, signoff, metric_table, and the
// compliance callout with its item list) stay addable but edit through the JSON escape hatch — honest,
// and it keeps this map from silently hiding a list an author cannot see.
export const COMPLIANCE_BLOCK_FIELDS: Record<string, FieldDef[]> = {
  cover: [
    { key: 'title', labelKey: 'studio.block.f.title', bilingual: true, multiline: true },
    { key: 'subtitle', labelKey: 'studio.block.f.subtitle', bilingual: true, multiline: true },
  ],
  section: [
    { key: 'level', labelKey: 'studio.block.f.level', select: ['1', '2', '3'] },
    { key: 'text', labelKey: 'studio.block.f.text', bilingual: true, multiline: true },
  ],
  lead: [{ key: 'text', labelKey: 'studio.block.f.text', bilingual: true, multiline: true }],
  flag: [{ key: 'text', labelKey: 'studio.block.f.text', bilingual: true, multiline: true }],
  helper: [{ key: 'text', labelKey: 'studio.block.f.text', bilingual: true, multiline: true }],
  footer_band: [{ key: 'text', labelKey: 'studio.block.f.text', bilingual: true, multiline: true }],
  metric: [
    { key: 'label', labelKey: 'studio.block.f.label', bilingual: true },
    { key: 'requirement', labelKey: 'studio.block.f.requirement' },
    { key: 'value', labelKey: 'studio.block.f.value', bilingual: true },
    { key: 'unit', labelKey: 'studio.block.f.unit', bilingual: true },
  ],
  page_break: [],
  // The structured kinds — friendly editors, no JSON. Shapes mirror services/documents BLOCK_SPEC and the
  // client DocBlockRenderer: bullets/snapshot/fields/references are label→detail pairs; clause is a number
  // + title + paragraphs; callout is a style + title + item list; table/evidence are cell grids; signoff
  // is a list of signatory lines; metric_table is rows of label/value/unit.
  bullets: [
    { key: 'items', labelKey: 'studio.block.f.items', kind: 'listPair', pair: ['studio.block.f.term', 'studio.block.f.detail'], multiline: true },
  ],
  clause: [
    { key: 'n', labelKey: 'studio.block.f.clauseNo' },
    { key: 'title', labelKey: 'studio.block.f.title', bilingual: true, multiline: true },
    { key: 'paras', labelKey: 'studio.block.f.paras', kind: 'listText', multiline: true },
  ],
  callout: [
    { key: 'kind', labelKey: 'studio.block.f.calloutKind', select: ['info', 'warn', 'success', 'danger'] },
    { key: 'title', labelKey: 'studio.block.f.title', bilingual: true },
    { key: 'items', labelKey: 'studio.block.f.items', kind: 'listText', multiline: true },
  ],
  references: [
    { key: 'title', labelKey: 'studio.block.f.title', bilingual: true },
    { key: 'items', labelKey: 'studio.block.f.items', kind: 'listPair', pair: ['studio.block.f.term', 'studio.block.f.detail'], multiline: true },
  ],
  snapshot: [
    { key: 'rows', labelKey: 'studio.block.f.rows', kind: 'listPair', pair: ['studio.block.f.label', 'studio.block.f.value'] },
  ],
  fields: [
    { key: 'rows', labelKey: 'studio.block.f.rows', kind: 'listPair', pair: ['studio.block.f.label', 'studio.block.f.value'] },
  ],
  signoff: [
    { key: 'roles', labelKey: 'studio.block.f.roles', kind: 'listText' },
  ],
  table: [
    { key: 'headers', labelKey: 'studio.block.f.headers', kind: 'listText' },
    { key: 'rows', labelKey: 'studio.block.f.rows', kind: 'table' },
  ],
  evidence: [
    { key: 'rows', labelKey: 'studio.block.f.rows', kind: 'table' },
  ],
  metric_table: [
    { key: 'title', labelKey: 'studio.block.f.title', bilingual: true },
    { key: 'rows', labelKey: 'studio.block.f.rows', kind: 'metricTable' },
  ],
};
// Ordered by how often an author reaches for them — the everyday prose blocks first.
export const COMPLIANCE_ADDABLE = [
  'section', 'lead', 'flag', 'helper', 'bullets', 'clause', 'callout', 'table', 'evidence',
  'snapshot', 'fields', 'references', 'signoff', 'cover', 'footer_band', 'page_break',
  'metric', 'metric_table',
];
export const COMPLIANCE_VOCAB: Vocabulary = {
  fields: COMPLIANCE_BLOCK_FIELDS, addable: COMPLIANCE_ADDABLE,
};

export const FIELD =
  'w-full rounded-lg border border-border-strong bg-surface-1 px-3 py-2 text-sm text-text-primary outline-none transition focus-ring';

// ── the ENVELOPE seam (Phase 2) ─────────────────────────────────────────────────────────────────────
// Two channels serialise a block differently. The admin/legal wire shape is a two-key `{ type, data }`;
// the client document-instance wire shape is a single-key `{ [type]: data }`. The internal editor always
// works on `{ type, data }`; a host with `kind` blocks converts at the boundary with these PURE, total
// isomorphisms. `kind = Object.keys(b)[0]` — the single key is the type, everything under it is the data.
export type Envelope = 'type-data' | 'kind';
export type KindBlock = Record<string, Record<string, unknown>>;

/** `{ [type]: data }` → `{ type, data }`. An empty object (no keys) degrades to a typeless empty block. */
export function kindToBlock(b: KindBlock): Block {
  const type = Object.keys(b)[0] ?? '';
  const data = (type ? b[type] : {}) ?? {};
  return { type, data: data as Record<string, unknown> };
}
/** `{ type, data }` → `{ [type]: data }`, the inverse of `kindToBlock`. */
export function blockToKind(b: Block): KindBlock { return { [b.type]: b.data }; }

/** Fold a flat `Block[]` into a single title-less implicit section, so `SectionPane` can edit an
 *  instance that has no sections. The inverse is simply `section.blocks`. */
export function flatSection(blocks: Block[]): Section {
  return { title_ar: '', title_en: '', blocks };
}

/** A textarea that grows with its content — no tiny fixed box, no manual dragging. Caps at a tall
 *  ceiling, then scrolls, so one long clause can't push the rest of the editor off-screen. */
export function AutoTextarea({
  value, onChange, dir, className,
}: { value: string; onChange: (v: string) => void; dir?: 'rtl' | 'ltr'; className?: string }) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const el = ref.current; if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 640)}px`;
  }, [value]);
  return (
    <textarea ref={ref} dir={dir} value={value} rows={2}
      onChange={(e) => onChange(e.target.value)}
      className={`${FIELD} resize-none overflow-hidden leading-relaxed ${className ?? ''}`} />
  );
}

/** The EN (or AR) side of a nested bilingual `{ en, ar }` Text value, if that's what `v` is. */
function bilingualGist(v: unknown): string {
  if (v && typeof v === 'object') {
    const o = v as Record<string, unknown>;
    if (typeof o.en === 'string') return o.en;
    if (typeof o.ar === 'string') return o.ar;
  }
  return '';
}

/** The `prefer` edition of a bilingual `{ en, ar }` Text, with NO fallback to the other language — so an
 *  index reading in Arabic shows the Arabic side (or nothing, prompting a translation), never the English. */
function editionGist(v: unknown, prefer: Edition): string {
  if (v && typeof v === 'object') {
    const o = v as Record<string, unknown>;
    if (typeof o[prefer] === 'string') return o[prefer] as string;
  }
  return '';
}

/** A one-line gist of a block for the index/accordion row. Reads both the legal flat convention
 *  (md_en/caption_en/ref) and the compliance nested-bilingual one (text/title/subtitle → {en, ar}).
 *  When `prefer` is given, the gist is shown in THAT edition only (no cross-language fallback), so a
 *  reader editing Arabic never sees an English gist bleeding through. */
export function blockPreview(b: Block, prefer?: Edition): string {
  const flat = prefer
    ? (b.data[`md_${prefer}`] ?? b.data[`caption_${prefer}`] ?? b.data.ref ?? '')
    : (b.data.md_en ?? b.data.md_ar ?? b.data.caption_en ?? b.data.ref ?? '');
  const nested = prefer
    ? (editionGist(b.data.text, prefer) || editionGist(b.data.title, prefer) || editionGist(b.data.subtitle, prefer))
    : (bilingualGist(b.data.text) || bilingualGist(b.data.title) || bilingualGist(b.data.subtitle));
  const v = typeof flat === 'string' && flat ? flat : nested;
  const s = typeof v === 'string' ? v.replace(/[#*`>_-]/g, '').replace(/\s+/g, ' ').trim() : '';
  return s ? (s.length > 80 ? `${s.slice(0, 80)}…` : s) : '';
}

// ── structured-block editors (compliance) — friendly, per-edition, NEVER raw JSON ────────────────────
// The one convention every structured cell shares: a bilingual `{ en, ar }` Text. These helpers + the
// small `BiCell` input are the atoms the list/grid editors below are composed from, so language is
// handled exactly like the scalar bilingual field (per edition, Arabic RTL, narrowed by `editions`).
type TextObj = { en?: string; ar?: string };
const asText = (v: unknown): TextObj => (v && typeof v === 'object' && !Array.isArray(v) ? (v as TextObj) : {});

/** One bilingual value edited across the given editions — the shared atom of every structured editor.
 *  Always an auto-growing textarea (never a single-line input), so a long cell — a section subtitle, a
 *  sentence in a bullet — is fully visible instead of clipped inside a box smaller than its text. */
function BiCell({ value, editions, onChange }: {
  value: unknown; editions: Edition[]; onChange: (t: TextObj) => void;
}) {
  const { t } = useBlockDocI18n();
  const cur = asText(value);
  return (
    <div className="grid grid-cols-2 gap-2 max-[32rem]:grid-cols-1">
      {editions.map((lang) => (
        <div key={lang} className="flex min-w-0 flex-col gap-0.5">
          {editions.length > 1 && (
            <span className="text-[0.6rem] uppercase tracking-wide text-text-muted">{t(`studio.block.${lang}`)}</span>
          )}
          <AutoTextarea value={cur[lang] ?? ''} dir={lang === 'ar' ? 'rtl' : 'ltr'}
            onChange={(v) => onChange({ ...cur, [lang]: v })} />
        </div>
      ))}
    </div>
  );
}

function AddButton({ onClick, label }: { onClick: () => void; label: string }) {
  return (
    <button type="button" onClick={onClick}
      className="inline-flex w-fit items-center gap-1 rounded-md border border-dashed border-border-strong px-2.5 py-1 text-xs font-medium text-text-secondary transition hover:border-accent hover:text-accent">
      <Plus size={13} aria-hidden /> {label}
    </button>
  );
}

/** The frame around one repeatable row: an index chip, the row body, and move/remove controls. */
function RepeatRow({ i, total, onMove, onRemove, children }: {
  i: number; total: number; onMove: (dir: -1 | 1) => void; onRemove: () => void; children: ReactNode;
}) {
  const { t } = useBlockDocI18n();
  return (
    <div className="flex items-start gap-2 rounded-lg border border-border-subtle bg-surface-2/40 p-2">
      <span className="mt-1 grid h-5 w-5 shrink-0 place-items-center rounded bg-surface-2 text-[0.65rem] font-semibold text-text-muted">{i + 1}</span>
      <div className="min-w-0 flex-1">{children}</div>
      <div className="flex shrink-0 flex-col gap-0.5 text-text-muted">
        <button type="button" onClick={() => onMove(-1)} disabled={i === 0} className="rounded p-0.5 hover:bg-surface-2 disabled:opacity-30" title={t('legal.moveUp')}><ChevronUp size={13} /></button>
        <button type="button" onClick={() => onMove(1)} disabled={i === total - 1} className="rounded p-0.5 hover:bg-surface-2 disabled:opacity-30" title={t('legal.moveDown')}><ChevronDown size={13} /></button>
        <button type="button" onClick={onRemove} className="rounded p-0.5 hover:bg-danger-soft hover:text-danger" title={t('studio.block.list.remove')}><Trash2 size={12} /></button>
      </div>
    </div>
  );
}

const asArray = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
function moveIn<T>(arr: T[], i: number, dir: -1 | 1): T[] {
  const j = i + dir; if (j < 0 || j >= arr.length) return arr;
  const n = [...arr]; [n[i], n[j]] = [n[j], n[i]]; return n;
}

/** An ordered list of bilingual Text (callout items, clause paragraphs, signoff signatories, table headers). */
function ListTextEditor({ value, editions, onChange }: {
  value: unknown; editions: Edition[]; onChange: (next: unknown[]) => void;
}) {
  const { t } = useBlockDocI18n();
  const items = asArray(value);
  return (
    <div className="flex flex-col gap-2">
      {items.map((it, i) => (
        <RepeatRow key={i} i={i} total={items.length} onMove={(d) => onChange(moveIn(items, i, d))}
          onRemove={() => onChange(items.filter((_, k) => k !== i))}>
          <BiCell value={it} editions={editions}
            onChange={(tv) => onChange(items.map((x, k) => (k === i ? tv : x)))} />
        </RepeatRow>
      ))}
      {items.length === 0 && <p className="px-1 text-xs text-text-muted">{t('studio.block.list.empty')}</p>}
      <AddButton onClick={() => onChange([...items, {}])} label={t('studio.block.list.add')} />
    </div>
  );
}

/** An ordered list of [Text, Text] rows (bullets, snapshot, fields, references) — two labelled columns. */
function ListPairEditor({ value, editions, cols, onChange }: {
  value: unknown; editions: Edition[]; cols: [string, string]; onChange: (next: unknown[]) => void;
}) {
  const { t } = useBlockDocI18n();
  const items = asArray(value);
  const pairOf = (it: unknown): [unknown, unknown] => (Array.isArray(it) ? [it[0], it[1]] : [it, '']);
  return (
    <div className="flex flex-col gap-2">
      {items.map((it, i) => {
        const [a, b] = pairOf(it);
        const set = (ai: 0 | 1, tv: TextObj) => {
          const p = pairOf(it); p[ai] = tv;
          onChange(items.map((x, k) => (k === i ? p : x)));
        };
        return (
          <RepeatRow key={i} i={i} total={items.length} onMove={(d) => onChange(moveIn(items, i, d))}
            onRemove={() => onChange(items.filter((_, k) => k !== i))}>
            <div className="grid gap-2 sm:grid-cols-2">
              <div className="flex flex-col gap-0.5">
                <span className="text-[0.65rem] font-medium uppercase tracking-wide text-text-muted">{t(cols[0])}</span>
                <BiCell value={a} editions={editions} onChange={(tv) => set(0, tv)} />
              </div>
              <div className="flex flex-col gap-0.5">
                <span className="text-[0.65rem] font-medium uppercase tracking-wide text-text-muted">{t(cols[1])}</span>
                <BiCell value={b} editions={editions} onChange={(tv) => set(1, tv)} />
              </div>
            </div>
          </RepeatRow>
        );
      })}
      {items.length === 0 && <p className="px-1 text-xs text-text-muted">{t('studio.block.list.empty')}</p>}
      <AddButton onClick={() => onChange([...items, ['', '']])} label={t('studio.block.list.add')} />
    </div>
  );
}

/** A grid of bilingual Text cells (table rows, evidence rows). Add/remove rows and columns; each cell is
 *  a per-edition bilingual input. Column count follows the widest row (min 1). */
function TableEditor({ value, editions, onChange }: {
  value: unknown; editions: Edition[]; onChange: (next: unknown[]) => void;
}) {
  const { t } = useBlockDocI18n();
  const rows = asArray(value).map((r) => asArray(r));
  const cols = Math.max(1, ...rows.map((r) => r.length), rows.length ? 0 : 1);
  const setCell = (ri: number, ci: number, tv: TextObj) =>
    onChange(rows.map((r, k) => (k === ri ? r.map((c, kc) => (kc === ci ? tv : c)) : r)));
  const addRow = () => onChange([...rows, Array.from({ length: cols }, () => ({}))]);
  const addCol = () => onChange(rows.map((r) => [...r, {}]));
  const removeRow = (ri: number) => onChange(rows.filter((_, k) => k !== ri));
  const removeCol = () => cols > 1 && onChange(rows.map((r) => r.slice(0, cols - 1)));
  return (
    <div className="flex flex-col gap-2">
      {rows.map((r, ri) => (
        <RepeatRow key={ri} i={ri} total={rows.length} onMove={(d) => onChange(moveIn(rows, ri, d))} onRemove={() => removeRow(ri)}>
          <div className="grid gap-2" style={{ gridTemplateColumns: `repeat(${Math.max(1, cols)}, minmax(0, 1fr))` }}>
            {Array.from({ length: cols }, (_, ci) => (
              <BiCell key={ci} value={r[ci]} editions={editions} onChange={(tv) => setCell(ri, ci, tv)} />
            ))}
          </div>
        </RepeatRow>
      ))}
      {rows.length === 0 && <p className="px-1 text-xs text-text-muted">{t('studio.block.list.empty')}</p>}
      <div className="flex flex-wrap items-center gap-2">
        <AddButton onClick={addRow} label={t('studio.block.list.addRow')} />
        <AddButton onClick={addCol} label={t('studio.block.list.addCol')} />
        {cols > 1 && (
          <button type="button" onClick={removeCol}
            className="inline-flex w-fit items-center gap-1 rounded-md border border-dashed border-border-strong px-2.5 py-1 text-xs font-medium text-text-muted transition hover:border-danger hover:text-danger">
            <Trash2 size={12} aria-hidden /> {t('studio.block.list.removeCol')}
          </button>
        )}
      </div>
    </div>
  );
}

/** Rows of { label, value, unit } bilingual Text (metric_table). */
function MetricTableEditor({ value, editions, onChange }: {
  value: unknown; editions: Edition[]; onChange: (next: unknown[]) => void;
}) {
  const { t } = useBlockDocI18n();
  const rows = asArray(value);
  const rowOf = (r: unknown): Record<string, unknown> => (r && typeof r === 'object' && !Array.isArray(r) ? (r as Record<string, unknown>) : {});
  const setField = (ri: number, key: string, tv: TextObj) =>
    onChange(rows.map((r, k) => (k === ri ? { ...rowOf(r), [key]: tv } : r)));
  const fields: [string, string][] = [['label', 'studio.block.f.label'], ['value', 'studio.block.f.value'], ['unit', 'studio.block.f.unit']];
  return (
    <div className="flex flex-col gap-2">
      {rows.map((r, ri) => (
        <RepeatRow key={ri} i={ri} total={rows.length} onMove={(d) => onChange(moveIn(rows, ri, d))}
          onRemove={() => onChange(rows.filter((_, k) => k !== ri))}>
          <div className="grid gap-2 sm:grid-cols-3">
            {fields.map(([key, labelKey]) => (
              <div key={key} className="flex flex-col gap-0.5">
                <span className="text-[0.65rem] font-medium uppercase tracking-wide text-text-muted">{t(labelKey)}</span>
                <BiCell value={rowOf(r)[key]} editions={editions} onChange={(tv) => setField(ri, key, tv)} />
              </div>
            ))}
          </div>
        </RepeatRow>
      ))}
      {rows.length === 0 && <p className="px-1 text-xs text-text-muted">{t('studio.block.list.empty')}</p>}
      <AddButton onClick={() => onChange([...rows, {}])} label={t('studio.block.list.add')} />
    </div>
  );
}

/** The section rail: document titles, the section list, and add-section. Shared so the modal and the
 *  Studio page present the same navigation. */
export function SectionRail({
  draft, active, onSelect, onEditDraft, onAddSection,
}: {
  draft: Draft; active: number; onSelect: (i: number) => void;
  onEditDraft: (fn: (d: Draft) => Draft) => void; onAddSection: () => void;
}) {
  const { t, locale } = useBlockDocI18n();
  const sectionLabel = (s: Section, i: number) =>
    (locale === 'ar' ? s.title_ar : s.title_en) || (locale === 'ar' ? s.title_en : s.title_ar) || `${t('legal.section')} ${i + 1}`;
  return (
    <div className="flex h-full flex-col gap-2">
      <div className="flex flex-col gap-1.5 border-b border-border-subtle pb-3">
        <input className={`${FIELD} py-1.5 text-sm`} placeholder={t('legal.f.titleEn')} value={draft.title_en}
          onChange={(e) => onEditDraft((d) => ({ ...d, title_en: e.target.value }))} />
        <input className={`${FIELD} py-1.5 text-sm`} dir="rtl" placeholder={t('legal.f.titleAr')} value={draft.title_ar}
          onChange={(e) => onEditDraft((d) => ({ ...d, title_ar: e.target.value }))} />
      </div>
      <div className="flex items-center justify-between px-1">
        <span className="text-xs font-medium uppercase tracking-wide text-text-muted">{t('legal.sections')}</span>
        <span className="text-xs text-text-muted">{draft.sections.length}</span>
      </div>
      <ol className="flex min-h-0 flex-1 flex-col gap-1 overflow-auto">
        {draft.sections.map((s, i) => {
          const on = i === active;
          return (
            <li key={i}>
              <button type="button" onClick={() => onSelect(i)}
                className={`group flex w-full items-center gap-2.5 rounded-xl px-2.5 py-2.5 text-start transition ${
                  on ? 'bg-accent-soft ring-1 ring-accent/25' : 'hover:bg-surface-2'}`}>
                <span className={`grid h-7 w-7 shrink-0 place-items-center rounded-lg text-xs font-semibold tabular-nums transition ${
                  on ? 'bg-accent text-accent-contrast shadow-sm' : 'bg-surface-2 text-text-muted group-hover:text-text-secondary'}`}>{i + 1}</span>
                <span className={`min-w-0 flex-1 truncate text-sm leading-snug ${on ? 'font-semibold text-accent-on-soft' : 'text-text-secondary'}`}>{sectionLabel(s, i)}</span>
                <span className={`shrink-0 rounded-full px-1.5 py-0.5 text-2xs tabular-nums ${on ? 'bg-accent/15 text-accent-on-soft' : 'bg-surface-2 text-text-muted'}`}>{s.blocks.length}</span>
              </button>
            </li>
          );
        })}
      </ol>
      <Button variant="outline" size="sm" onClick={onAddSection} className="mt-1 w-full">
        <Plus size={14} className="me-1" /> {t('legal.addSection')}
      </Button>
    </div>
  );
}

export function SectionPane({
  section, index, total, onChange, onMove, onRemove, vocab = LEGAL_VOCAB, editions = ALL_EDITIONS, flat = false,
}: {
  section: Section; index: number; total: number;
  onChange: (s: Section) => void; onMove: (dir: -1 | 1) => void; onRemove: () => void;
  // Which block family this document uses. Defaults to legal so LegalPage is untouched (#606); the
  // Studio policy editor passes COMPLIANCE_VOCAB.
  vocab?: Vocabulary;
  // Which languages the bilingual field editor offers (Phase 2). Admin authors both (the default); a
  // single-edition tenant narrows it so its author sees only the editions it produces.
  editions?: Edition[];
  // FLAT MODE (Phase 2): a title-less implicit section for a document instance with no sections. Hides
  // the section header (number/move/remove) and the title inputs — just the block list + add bar.
  flat?: boolean;
}) {
  const { t, locale } = useBlockDocI18n();
  // null = every block collapsed. The accordion is not exclusive-with-a-floor: clicking the open block
  // closes it, so an author can have them ALL shut (a clean overview) — not "always exactly one open".
  const [openBlock, setOpenBlock] = useState<number | null>(0);
  // Drag-and-drop reordering (#894). Only COLLAPSED blocks are draggable, so dragging never fights inline
  // text selection in the open editor. `dragFrom` is the picked-up index; `dragOver` highlights the drop
  // target. Native HTML5 DnD is direction-agnostic, so this is correct in both LTR and RTL.
  const dragFrom = useRef<number | null>(null);
  const [dragOver, setDragOver] = useState<number | null>(null);
  // Which gap's "+" insert menu is open (index = insert-at position); null = none.
  const [openGap, setOpenGap] = useState<number | null>(null);
  const heading = (locale === 'ar' ? section.title_ar : section.title_en) || section.title_en || section.title_ar || `${t('legal.section')} ${index + 1}`;
  const setBlock = (bi: number, b: Block) => {
    const blocks = section.blocks.slice(); blocks[bi] = b; onChange({ ...section, blocks });
  };
  const addBlock = (type: string) => {
    onChange({ ...section, blocks: [...section.blocks, { type, data: {} }] });
    setOpenBlock(section.blocks.length); // open the one just added
  };
  const moveBlock = (bi: number, dir: -1 | 1) => {
    const j = bi + dir; if (j < 0 || j >= section.blocks.length) return;
    const blocks = section.blocks.slice(); [blocks[bi], blocks[j]] = [blocks[j], blocks[bi]];
    onChange({ ...section, blocks }); setOpenBlock(j);
  };
  const removeBlock = (bi: number) => {
    onChange({ ...section, blocks: section.blocks.filter((_, i) => i !== bi) });
    // Keep whatever was open pointing at the same block; the removed one (or none) collapses to null.
    setOpenBlock((o) => (o === null ? null : o === bi ? null : o > bi ? o - 1 : o));
  };
  // Move a block from one index to another (drag-and-drop). Splice-out then splice-in preserves order.
  const moveBlockTo = (from: number, to: number) => {
    if (from === to || from < 0 || to < 0 || from >= section.blocks.length || to >= section.blocks.length) return;
    const blocks = section.blocks.slice();
    const [m] = blocks.splice(from, 1);
    blocks.splice(to, 0, m);
    onChange({ ...section, blocks });
    setOpenBlock(to);
  };
  // Insert a fresh block of `type` at a specific gap (the "+" between blocks), open it, close the menu.
  const insertBlock = (at: number, type: string) => {
    const blocks = section.blocks.slice();
    blocks.splice(at, 0, { type, data: {} });
    onChange({ ...section, blocks });
    setOpenBlock(at);
    setOpenGap(null);
  };
  // A thin gap between blocks: reveals a "+" on hover; clicking it opens an inline palette that inserts a
  // block of the chosen kind AT THIS POSITION (not just appended), so the author builds the page in place.
  const gapBar = (at: number): ReactNode => (
    <div className="group/gap relative flex min-h-[0.75rem] items-center justify-center">
      {openGap === at ? (
        <div className="z-10 flex flex-wrap items-center gap-1 rounded-lg border border-accent/40 bg-surface-1 px-2 py-1 shadow-sm">
          {vocab.addable.map((type) => (
            <button key={type} type="button" onClick={() => insertBlock(at, type)}
              className="rounded-md border border-border-subtle bg-surface-1 px-2 py-0.5 text-xs text-text-secondary transition hover:border-accent hover:text-text-primary">
              {blockKindLabel(t, type)}
            </button>
          ))}
          <button type="button" onClick={() => setOpenGap(null)} className="ms-1 grid h-5 w-5 place-items-center rounded text-text-muted hover:text-danger" title={t('legal.cancel')} aria-label={t('legal.cancel')}>×</button>
        </div>
      ) : (
        <button type="button" onClick={() => setOpenGap(at)} title={t('legal.addBlock')} aria-label={t('legal.addBlock')}
          className="grid h-4 w-4 place-items-center rounded-full border border-dashed border-border-subtle bg-surface-1 text-text-muted opacity-0 transition hover:border-accent hover:text-accent group-hover/gap:opacity-100">
          <Plus size={11} />
        </button>
      )}
    </div>
  );
  // reset to the first block whenever the section itself changes (nav in the rail)
  useEffect(() => { setOpenBlock(0); }, [index]);
  // Alt+↑ / Alt+↓ jump between blocks without leaving the keyboard (from "all closed", ↓ opens the first).
  const onKey = (e: React.KeyboardEvent) => {
    if (!e.altKey || section.blocks.length === 0) return;
    const last = section.blocks.length - 1;
    if (e.key === 'ArrowDown') { e.preventDefault(); setOpenBlock((o) => (o === null ? 0 : Math.min(last, o + 1))); }
    if (e.key === 'ArrowUp') { e.preventDefault(); setOpenBlock((o) => (o === null ? last : Math.max(0, o - 1))); }
  };

  return (
    <div className="flex flex-col gap-4">
      {/* section header: number, titles, and section-level controls — hidden in flat mode */}
      {!flat && (
        <div className="flex items-center gap-2">
          <h2 className="min-w-0 flex-1 truncate text-base font-semibold text-text-primary">{heading}</h2>
          <div className="flex shrink-0 items-center gap-1 text-text-muted">
            <button type="button" onClick={() => onMove(-1)} disabled={index === 0} className="rounded p-1 hover:bg-surface-2 disabled:opacity-30" title={t('legal.moveUp')}><ChevronUp size={16} /></button>
            <button type="button" onClick={() => onMove(1)} disabled={index === total - 1} className="rounded p-1 hover:bg-surface-2 disabled:opacity-30" title={t('legal.moveDown')}><ChevronDown size={16} /></button>
            <button type="button" onClick={onRemove} className="rounded p-1 hover:bg-danger-soft hover:text-danger" title={t('legal.removeSection')}><Trash2 size={15} /></button>
          </div>
        </div>
      )}

      {!flat && (
        <div className="grid grid-cols-2 gap-3 max-[42rem]:grid-cols-1">
          <label className="flex flex-col gap-1">
            <span className="text-xs text-text-muted">{t('legal.f.titleEn')}</span>
            <input className={FIELD} value={section.title_en} onChange={(e) => onChange({ ...section, title_en: e.target.value })} />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs text-text-muted">{t('legal.f.titleAr')}</span>
            <input className={FIELD} dir="rtl" value={section.title_ar} onChange={(e) => onChange({ ...section, title_ar: e.target.value })} />
          </label>
        </div>
      )}

      <div className="flex flex-col gap-1" onKeyDown={onKey}>
        {section.blocks.length > 1 && (
          <p className="px-1 text-xs text-text-muted">{t('legal.blockNavHint')}</p>
        )}
        {section.blocks.map((b, bi) => (
          <Fragment key={bi}>
            {gapBar(bi)}
            <div
              draggable={bi !== openBlock}
              onDragStart={(e) => { dragFrom.current = bi; e.dataTransfer.effectAllowed = 'move'; }}
              onDragOver={(e) => { if (dragFrom.current !== null) { e.preventDefault(); if (dragOver !== bi) setDragOver(bi); } }}
              onDrop={(e) => { e.preventDefault(); if (dragFrom.current !== null) moveBlockTo(dragFrom.current, bi); dragFrom.current = null; setDragOver(null); }}
              onDragEnd={() => { dragFrom.current = null; setDragOver(null); }}
              className={`rounded-xl transition ${dragOver === bi && dragFrom.current !== bi ? 'ring-2 ring-accent/50' : ''}`}
            >
              <BlockEditor block={b} index={bi} total={section.blocks.length} vocab={vocab} editions={editions}
                open={bi === openBlock} onOpen={() => setOpenBlock((o) => (o === bi ? null : bi))}
                onChange={(nb) => setBlock(bi, nb)} onMove={(dir) => moveBlock(bi, dir)} onRemove={() => removeBlock(bi)} />
            </div>
          </Fragment>
        ))}
        {gapBar(section.blocks.length)}
        {section.blocks.length === 0 && (
          <p className="rounded-lg border border-dashed border-border-subtle px-3 py-4 text-center text-xs text-text-muted">{t('legal.noBlocks')}</p>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-1.5 rounded-lg bg-surface-2/40 px-3 py-2">
        <span className="text-xs text-text-muted">{t('legal.addBlock')}:</span>
        {vocab.addable.map((type) => (
          <button key={type} type="button" onClick={() => addBlock(type)}
            className="rounded-md border border-border-subtle bg-surface-1 px-2 py-0.5 text-xs text-text-secondary transition hover:border-accent hover:text-text-primary">
            {blockKindLabel(t, type)}
          </button>
        ))}
      </div>
    </div>
  );
}

// ── the DOCUMENT WORKSPACE (flat docs): an index rail + a one-block editing pane ──────────────────────
// The instance editor used to stack every block in one column — a wall the user had to scroll. This is
// the same shape the admin's legal editor uses (a rail beside a focused pane): the RAIL is the document's
// table of contents (every block, its kind + a one-line gist in the reading edition), and the PANE edits
// exactly the selected block. The two are separate components so a host can drop the rail into a Modal's
// `sidebar` slot and the pane into its body. Operates on a flat `Block[]` (the instance shape).

/** The document's table of contents — every block as a selectable row (kind + edition-aware gist), plus an
 *  add-block affordance. `prefer` shows the gist in the edition being edited, never the other language. */
export function BlockIndexRail({
  blocks, active, onSelect, vocab, onAdd, prefer, onReorder,
}: {
  blocks: Block[]; active: number; onSelect: (i: number) => void;
  vocab?: Vocabulary; onAdd?: (type: string) => void; prefer?: Edition;
  // When given, rail rows become drag-and-drop reorderable (the flat client instance editor). Reorders
  // the underlying block list from `from` to `to`.
  onReorder?: (from: number, to: number) => void;
}) {
  const { t } = useBlockDocI18n();
  const [adding, setAdding] = useState(false);
  const railFrom = useRef<number | null>(null);
  const [railOver, setRailOver] = useState<number | null>(null);
  return (
    <div className="flex h-full flex-col gap-2">
      <div className="flex items-center justify-between px-1">
        <span className="text-xs font-semibold uppercase tracking-wide text-text-muted">{t('studio.block.contents')}</span>
        <span className="text-xs tabular-nums text-text-muted">{blocks.length}</span>
      </div>
      <ol className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-auto">
        {blocks.map((b, i) => {
          const on = i === active;
          const gist = blockPreview(b, prefer);
          return (
            <li key={i}
              draggable={!!onReorder}
              onDragStart={onReorder ? (e) => { railFrom.current = i; e.dataTransfer.effectAllowed = 'move'; } : undefined}
              onDragOver={onReorder ? (e) => { if (railFrom.current !== null) { e.preventDefault(); if (railOver !== i) setRailOver(i); } } : undefined}
              onDrop={onReorder ? (e) => { e.preventDefault(); if (railFrom.current !== null && railFrom.current !== i) onReorder(railFrom.current, i); railFrom.current = null; setRailOver(null); } : undefined}
              onDragEnd={onReorder ? () => { railFrom.current = null; setRailOver(null); } : undefined}
              className={onReorder && railOver === i && railFrom.current !== i ? 'rounded-xl ring-2 ring-accent/50' : undefined}>
              <button type="button" onClick={() => onSelect(i)}
                className={`group flex w-full items-start gap-2.5 rounded-xl px-2.5 py-2 text-start transition ${on ? 'bg-accent-soft ring-1 ring-accent/25' : 'hover:bg-surface-2'}`}>
                <span className={`mt-0.5 grid h-6 w-6 shrink-0 place-items-center rounded-lg text-[0.65rem] font-semibold tabular-nums transition ${on ? 'bg-accent text-accent-contrast shadow-sm' : 'bg-surface-2 text-text-muted group-hover:text-text-secondary'}`}>{i + 1}</span>
                <span className="min-w-0 flex-1">
                  <span className={`block text-xs font-semibold ${on ? 'text-accent-on-soft' : 'text-text-secondary'}`}>{blockKindLabel(t, b.type)}</span>
                  <span className="mt-0.5 block truncate text-2xs text-text-muted">{gist || t('legal.emptyBlock')}</span>
                </span>
              </button>
            </li>
          );
        })}
        {blocks.length === 0 && <li className="px-2 py-3 text-xs text-text-muted">{t('legal.noBlocks')}</li>}
      </ol>
      {vocab && onAdd && (
        <div className="border-t border-border-subtle pt-2">
          {adding ? (
            <div className="flex flex-wrap gap-1">
              {vocab.addable.map((type) => (
                <button key={type} type="button" onClick={() => { onAdd(type); setAdding(false); }}
                  className="rounded-md border border-border-subtle bg-surface-1 px-2 py-0.5 text-2xs text-text-secondary transition hover:border-accent hover:text-text-primary">
                  {blockKindLabel(t, type)}
                </button>
              ))}
            </div>
          ) : (
            <Button variant="outline" size="sm" onClick={() => setAdding(true)} className="w-full">
              <Plus size={14} className="me-1" /> {t('legal.addBlock')}
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

/** The editing pane: the ONE selected block, expanded, with its move/remove controls. Keyed by index so
 *  switching blocks resets the editor's own local state (no stale JSON fallback bleeding across blocks). */
export function BlockWorkspace({
  blocks, active, onActiveChange, onChange, vocab = LEGAL_VOCAB, editions = ALL_EDITIONS,
}: {
  blocks: Block[]; active: number; onActiveChange: (i: number) => void;
  onChange: (blocks: Block[]) => void; vocab?: Vocabulary; editions?: Edition[];
}) {
  const { t } = useBlockDocI18n();
  if (blocks.length === 0) {
    return <p className="rounded-lg border border-dashed border-border-subtle px-3 py-10 text-center text-sm text-text-muted">{t('legal.noBlocks')}</p>;
  }
  const i = Math.min(Math.max(0, active), blocks.length - 1);
  const setBlock = (nb: Block) => onChange(blocks.map((x, k) => (k === i ? nb : x)));
  const move = (dir: -1 | 1) => {
    const j = i + dir; if (j < 0 || j >= blocks.length) return;
    const n = blocks.slice(); [n[i], n[j]] = [n[j], n[i]]; onChange(n); onActiveChange(j);
  };
  const remove = () => { onChange(blocks.filter((_, k) => k !== i)); onActiveChange(Math.max(0, i - 1)); };
  return (
    <BlockEditor key={i} block={blocks[i]} index={i} total={blocks.length} vocab={vocab} editions={editions}
      open onOpen={() => {}} onChange={setBlock} onMove={move} onRemove={remove} />
  );
}

// ── THE SHARED DOCUMENT EDITOR — one component both channels use ──────────────────────────────────────
// A self-contained editor: the index rail beside a focused editing pane, with a LANGUAGE VIEW filter on
// top. It manages its own selection + language state and renders its own two-column layout, so a host
// just gives it `blocks` + `onChange` and drops it into a roomy modal body — no wiring of rail/pane/lang
// per app. The language filter answers a real need: a customer who works only in Arabic hides English
// entirely (and vice-versa), or shows both side by side. Operates on the flat `Block[]` instance shape.
export function BlockDocEditor({
  blocks, onChange, editions = ALL_EDITIONS, vocab = LEGAL_VOCAB, locale,
}: {
  blocks: Block[]; onChange: (blocks: Block[]) => void;
  editions?: Edition[]; vocab?: Vocabulary; locale?: Edition;
}) {
  const { t, locale: ctxLocale } = useBlockDocI18n();
  const reading = locale ?? ctxLocale;
  const [active, setActive] = useState(0);
  // Default to the reader's own edition when the document supports it — someone who never uses English
  // shouldn't have to look at it. `both` shows the two side by side.
  const [view, setView] = useState<Edition | 'both'>(editions.includes(reading) ? reading : editions[0]);
  const effectiveView: Edition | 'both' = view === 'both' || editions.includes(view) ? view : editions[0];
  const shown: Edition[] = effectiveView === 'both' ? editions : [effectiveView];
  const prefer: Edition = effectiveView === 'both' ? reading : effectiveView;

  // Language options: each supported edition, plus "both" when there is more than one to choose between.
  const opts = editions.length > 1
    ? [...editions.map((e) => ({ value: e as Edition | 'both', label: t(`studio.block.${e}`) })),
       { value: 'both' as Edition | 'both', label: t('studio.block.both') }]
    : [];

  return (
    <div className="flex h-[72vh] gap-4 max-[46rem]:h-auto max-[46rem]:flex-col">
      <aside className="w-60 shrink-0 overflow-y-auto rounded-xl border border-border-subtle bg-surface-2/40 p-2 max-[46rem]:max-h-52 max-[46rem]:w-full">
        <BlockIndexRail blocks={blocks} active={active} onSelect={setActive} vocab={vocab} prefer={prefer}
          onAdd={(type) => { onChange([...blocks, { type, data: {} }]); setActive(blocks.length); }}
          onReorder={(from, to) => { const n = blocks.slice(); const [m] = n.splice(from, 1); n.splice(to, 0, m); onChange(n); setActive(to); }} />
      </aside>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-3 overflow-y-auto pe-1">
        {opts.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border-subtle bg-surface-2/40 px-3 py-2">
            <span className="text-xs font-medium text-text-muted">{t('studio.block.viewLang')}</span>
            <Segmented ariaLabel={t('studio.block.viewLang')} value={effectiveView}
              onChange={(v) => setView(v)} options={opts} />
          </div>
        )}
        <BlockWorkspace blocks={blocks} active={active} onActiveChange={setActive}
          onChange={onChange} vocab={vocab} editions={shown} />
      </div>
    </div>
  );
}

function BlockEditor({
  block, index, total, open, onOpen, onChange, onMove, onRemove, vocab = LEGAL_VOCAB, editions = ALL_EDITIONS,
}: {
  block: Block; index: number; total: number; open: boolean; onOpen: () => void;
  onChange: (b: Block) => void; onMove: (dir: -1 | 1) => void; onRemove: () => void;
  vocab?: Vocabulary;
  editions?: Edition[];
}) {
  const { t } = useBlockDocI18n();
  const fields = vocab.fields[block.type];
  const [jsonText, setJsonText] = useState(() => JSON.stringify(block.data, null, 2));
  const [jsonErr, setJsonErr] = useState('');
  const setField = (key: string, value: string) => onChange({ ...block, data: { ...block.data, [key]: value } });
  // A bilingual field writes into a nested `{ en, ar }` object, preserving the other language.
  const setLang = (key: string, lang: 'en' | 'ar', value: string) => {
    const prev = (block.data[key] && typeof block.data[key] === 'object')
      ? (block.data[key] as Record<string, unknown>) : {};
    onChange({ ...block, data: { ...block.data, [key]: { ...prev, [lang]: value } } });
  };
  const langVal = (key: string, lang: 'en' | 'ar'): string => {
    const o = block.data[key];
    if (o && typeof o === 'object' && typeof (o as Record<string, unknown>)[lang] === 'string') {
      return (o as Record<string, string>)[lang];
    }
    return '';
  };

  // COLLAPSED: a compact row with a gist. One block edits at a time — the workspace stays calm.
  if (!open) {
    const gist = blockPreview(block);
    return (
      <button type="button" onClick={onOpen}
        className="group flex w-full items-center gap-2 rounded-xl border border-border-subtle bg-surface-1 px-3 py-2 text-start transition hover:border-accent/50 hover:bg-surface-2/40">
        <ChevronRight size={15} className="shrink-0 text-text-muted transition group-hover:text-accent" />
        <span className="grid h-5 w-5 shrink-0 place-items-center rounded bg-surface-2 text-[0.7rem] font-semibold text-text-muted">{index + 1}</span>
        <Badge tone="info">{blockKindLabel(t, block.type)}</Badge>
        <span className="min-w-0 flex-1 truncate text-sm text-text-secondary">{gist || <span className="text-text-muted">{t('legal.emptyBlock')}</span>}</span>
      </button>
    );
  }

  // EXPANDED: the full editor, with auto-growing text areas.
  return (
    <div className="rounded-xl border border-accent/40 bg-surface-1 p-3 shadow-sm ring-1 ring-accent/10">
      <div className="mb-2 flex items-center justify-between">
        <button type="button" onClick={onOpen} className="flex items-center gap-1.5 text-text-muted">
          <GripVertical size={14} />
          <span className="grid h-5 w-5 place-items-center rounded bg-accent text-[0.7rem] font-semibold text-accent-contrast">{index + 1}</span>
          <Badge tone="info">{blockKindLabel(t, block.type)}</Badge>
        </button>
        <div className="flex items-center gap-1 text-text-muted">
          <button type="button" onClick={() => onMove(-1)} disabled={index === 0} className="rounded p-0.5 hover:bg-surface-2 disabled:opacity-30" title={t('legal.moveUp')}><ChevronUp size={14} /></button>
          <button type="button" onClick={() => onMove(1)} disabled={index === total - 1} className="rounded p-0.5 hover:bg-surface-2 disabled:opacity-30" title={t('legal.moveDown')}><ChevronDown size={14} /></button>
          <button type="button" onClick={onRemove} className="rounded p-0.5 hover:bg-danger-soft hover:text-danger" title={t('legal.removeSection')}><Trash2 size={13} /></button>
        </div>
      </div>

      {fields ? (
        fields.length === 0 ? (
          <p className="text-xs text-text-muted">{t('legal.noFields')}</p>
        ) : (
          <div className="flex flex-col gap-2">
            {fields.map((f) => {
              // STRUCTURED editors (compliance lists/grids) — a friendly editor, never raw JSON.
              if (f.kind) {
                const setStructured = (next: unknown) => onChange({ ...block, data: { ...block.data, [f.key]: next } });
                return (
                  <div key={f.key} className="flex flex-col gap-1.5">
                    <span className="text-xs font-medium text-text-secondary">{t(f.labelKey)}</span>
                    {f.kind === 'listText' && (
                      <ListTextEditor value={block.data[f.key]} editions={editions} onChange={setStructured} />
                    )}
                    {f.kind === 'listPair' && (
                      <ListPairEditor value={block.data[f.key]} editions={editions}
                        cols={f.pair ?? ['studio.block.f.label', 'studio.block.f.value']} onChange={setStructured} />
                    )}
                    {f.kind === 'table' && (
                      <TableEditor value={block.data[f.key]} editions={editions} onChange={setStructured} />
                    )}
                    {f.kind === 'metricTable' && (
                      <MetricTableEditor value={block.data[f.key]} editions={editions} onChange={setStructured} />
                    )}
                  </div>
                );
              }
              const val = typeof block.data[f.key] === 'string' ? (block.data[f.key] as string) : '';
              const rtl = f.key.endsWith('_ar');
              if (f.bilingual) {
                // One label, two inputs — English (LTR) and Arabic (RTL) sides of a `{ en, ar }` Text.
                // `editions` narrows which sides show (Phase 2); admin passes both.
                return (
                  <div key={f.key} className="flex flex-col gap-1">
                    <span className="text-xs text-text-muted">{t(f.labelKey)}</span>
                    <div className="grid grid-cols-2 gap-2 max-[36rem]:grid-cols-1">
                      {editions.map((lang) => (
                        <div key={lang} className="flex flex-col gap-0.5">
                          <span className="text-[0.65rem] uppercase tracking-wide text-text-muted">{t(`studio.block.${lang}`)}</span>
                          {/* Always an auto-growing textarea — a bilingual value (a title, a subtitle, a
                              label) is never clipped in a box smaller than its text, whatever its length. */}
                          <AutoTextarea value={langVal(f.key, lang)} dir={lang === 'ar' ? 'rtl' : 'ltr'}
                            onChange={(v) => setLang(f.key, lang, v)} />
                          </div>
                      ))}
                    </div>
                  </div>
                );
              }
              if (f.select) {
                // Coerce the stored value to a string for display: `level` is an int on disk (2), so
                // reading only strings would show a loaded level:2 as the first option. The value is
                // written back as a string here; the on-disk mapping layer coerces known-int fields.
                const cur = block.data[f.key] != null ? String(block.data[f.key]) : '';
                return (
                  <label key={f.key} className="flex flex-col gap-0.5">
                    <span className="text-xs text-text-muted">{t(f.labelKey)}</span>
                    <select className={FIELD} value={cur || f.select[0]} onChange={(e) => setField(f.key, e.target.value)}>
                      {f.select.map((o) => <option key={o} value={o}>{o}</option>)}
                    </select>
                  </label>
                );
              }
              return (
                <label key={f.key} className="flex flex-col gap-0.5">
                  <span className="text-xs text-text-muted">{t(f.labelKey)}</span>
                  {f.multiline ? (
                    <AutoTextarea value={val} dir={rtl ? 'rtl' : 'ltr'} onChange={(v) => setField(f.key, v)} />
                  ) : (
                    <input className={FIELD} dir={rtl ? 'rtl' : 'ltr'} value={val} onChange={(e) => setField(f.key, e.target.value)} />
                  )}
                </label>
              );
            })}
          </div>
        )
      ) : (
        <div className="flex flex-col gap-1">
          <span className="text-xs text-text-muted">{t('legal.rawData')}</span>
          <AutoTextarea value={jsonText} className="font-mono text-xs"
            onChange={(v) => {
              setJsonText(v);
              try { onChange({ ...block, data: JSON.parse(v) }); setJsonErr(''); }
              catch { setJsonErr(t('legal.badJson')); }
            }} />
          {jsonErr && <span className="text-xs text-danger">{jsonErr}</span>}
        </div>
      )}
    </div>
  );
}
