// #794 — the DOCUMENTS block renderer. A sibling of BlockRenderer (the legal `{type,data}` model), built
// for the documents-service block VOCABULARY: single-key nodes `{ kind: {…fields} }` — cover, section,
// lead, clause, table, callout, … (services/documents/app/core/blocks.py BLOCK_SPEC). The two block
// models are genuinely different shapes, so they cannot share one registry; this file follows the SAME
// idioms as BlockRenderer — a `RENDERERS` map keyed by block kind, an unknown kind degrades to a quiet
// fallback, and every value is rendered as React TEXT (never innerHTML), so a `{{TOKEN}}` or a stray
// '<script>' in authored content is inert.
//
// {{PLACEHOLDER}} tokens are resolved LIVE against the tenant's saved answers (the smart-fill values): a
// filled one renders as its value, an unfilled one renders as a subtle highlighted chip (the humanised
// key), so the reader sees a real document with exactly what is still to be filled — never a raw token.
import { createElement, useEffect, useRef, useState, type ReactNode } from 'react';
import { Plus, Trash2, GripVertical } from 'lucide-react';
import { inlineMarks } from './inlineMarks';
// Unified renderer (#892): legacy legal blocks arrive namespaced as `legal_*` and are delegated to the
// legal renderer, so this ONE renderer serves both document families without duplicating the markdown engine.
import { BlockView } from './BlockRenderer';
import { legalKindBase } from './legalToDocBlocks';

// #844 — Word-like inline table editing in quick-edit mode. The host passes these ops; the renderer wires
// them to editable cells + hover controls. Row/col indices are into the block's own grid.
export type EditOps = {
  onCell: (blockIndex: number, row: number, col: number, value: string) => void;
  addRow: (blockIndex: number) => void;
  addCol: (blockIndex: number) => void;
  delRow: (blockIndex: number, row: number) => void;
  delCol: (blockIndex: number, col: number) => void;
  // #844 — free-text editing of any prose field. `path` addresses the Text node inside the block
  // (e.g. ['text'], ['paras', 2], ['items', 1, 0]); the host writes the value into the shown edition.
  onText: (blockIndex: number, path: (string | number)[], value: string) => void;
  // #942 — BLOCK-level authoring, on the page itself (no separate editor). Optional: present ⇒ the read
  // page grows drag handles, a delete on each block, and "+" insert gaps with a small palette — Word-like,
  // in place. Absent ⇒ inline text/table editing only (a document with a fixed block set).
  moveBlock?: (from: number, to: number) => void;
  insertBlock?: (at: number, kind: string) => void;
  removeBlock?: (blockIndex: number) => void;
};

// #942 — the insertable block kinds offered by the on-page "+" palette, with localized labels. The host
// supplies them (it owns the vocabulary + i18n); the renderer stays label-agnostic. `chrome` carries the
// control tooltips so the shared renderer needs no dictionary of its own.
export type BlockChrome = {
  types: { kind: string; label: string }[];
  addLabel: string; deleteLabel: string; dragLabel: string;
};

// A contenteditable cell. Uncontrolled while typing (so the caret never jumps); commits on blur. Each
// {{TOKEN}} renders as an INERT clickable chip (same as prose) — clicking it opens the placeholder fill,
// so a `{{BOARD_BODY}}` inside a table cell is never raw, non-editable text. The effect re-syncs only when
// the EXTERNAL value changes to something the DOM does not already show. `richHtml`/`richText` are hoisted.
function EditableCell({ value, dir, ctx, onCommit }: { value: string; dir?: 'rtl' | 'ltr'; ctx: Ctx; onCommit: (v: string) => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (el && el.getAttribute('data-src') !== value) { el.innerHTML = richHtml(value, ctx); el.setAttribute('data-src', value); }
  });
  const commit = () => {
    const el = ref.current; if (!el) return;
    const t = richText(el);
    if (t !== value) { el.setAttribute('data-src', t); onCommit(t); }
  };
  const onClick = (e: { target: EventTarget | null; preventDefault: () => void }) => {
    const chip = (e.target as HTMLElement).closest?.('[data-token]');
    if (chip && ctx.onEdit) { e.preventDefault(); ctx.onEdit(chip.getAttribute('data-token')!); }
  };
  return (
    <div ref={ref} contentEditable suppressContentEditableWarning dir={dir} onBlur={commit} onClick={onClick}
      className="min-h-[1.5em] min-w-[3rem] rounded-sm px-1 outline-none focus:bg-[color:var(--accent)]/5 focus:ring-1 focus:ring-[color:var(--accent)]/30" />
  );
}

export type DocBlock = Record<string, Record<string, unknown>>;
type Locale = 'en' | 'ar';
/** key → the tenant's saved answer in the reading edition, or undefined when it is still to be filled. */
export type ResolveFn = (key: string) => string | undefined;

interface Ctx {
  locale: Locale; resolve?: ResolveFn; unfilled: string;
  // The human label for an UNFILLED placeholder, in the reading edition — so an Arabic document shows the
  // Arabic label ("جهة الحوكمة / الإشراف"), not the humanised English KEY ("Board Body"). Falls back to
  // the humanised key when a placeholder has no registered label.
  labelOf?: (key: string) => string | undefined;
  // #838 — when set, every {{PLACEHOLDER}} renders as a clickable chip; clicking asks the host to fill
  // that value inline. `editLabel` is the hover hint. Absent ⇒ the plain read rendering (marks/values).
  onEdit?: (key: string) => void; editLabel?: string;
  // #842 — a per-row table CELL whose whole content is a lone `[fill-in]` is a blank the customer fills
  // in the block editor (not a shared {{TOKEN}}). When `onEditCell` is set, such a cell renders as the
  // SAME orange fill-in chip as an unfilled token — one consistent look — and clicking it opens editing.
  // Absent ⇒ the bracket prints as literal text (a signed / read-only document must not offer to edit).
  onEditCell?: () => void;
  // #842 — keys the PLATFORM sets, not the customer (derived: EFFECTIVE_DATE, VERSION — KD-45). They
  // resolve live but must NOT invite a manual fill; they render as clear read-only values, never a chip.
  readonlyKeys?: Set<string>;
  autoLabel?: string;   // tooltip on a platform-set value, e.g. "set automatically"
  edit?: EditOps;       // #844 — present ⇒ quick-edit mode: tables get editable cells + row/col controls
  blockIndex?: number;  // this block's index in the document, so edit ops address the right grid
}

// A grid table with Word-like editing: editable cells, a "+ column" rail on the trailing edge, a "+ row"
// button under the last row, and a delete on each row/column. `headerRow` = the first grid row is column
// headers (true for `table`, false for the headerless `evidence` register).
function EditableGrid({ rows, ctx, headerRow }: { rows: unknown[]; ctx: Ctx; headerRow: boolean }): ReactNode {
  const bi = ctx.blockIndex ?? 0;
  const e = ctx.edit!;
  const dir = ctx.locale === 'ar' ? 'rtl' : 'ltr';
  const ncols = Math.max(1, ...rows.map((r) => arr(r).length));
  return (
    <div className="group/tbl relative my-2">
      <div className="legal-table-wrap">
        <table className="legal-table w-full">
          <tbody>
            {rows.map((row, ri) => (
              <tr key={ri} className="group/row">
                {Array.from({ length: ncols }).map((_, ci) => {
                  const Tag = (headerRow && ri === 0 ? 'th' : 'td') as 'td';
                  return (
                    <Tag key={ci}>
                      <EditableCell value={pick(arr(row)[ci], ctx.locale)} dir={dir} ctx={ctx}
                        onCommit={(v) => e.onCell(bi, ri, ci, v)} />
                    </Tag>
                  );
                })}
                <td className="w-8 border-0 bg-transparent p-0 align-middle">
                  {rows.length > 1 && (
                    <button type="button" onClick={() => e.delRow(bi, ri)} title="حذف الصف"
                      className="opacity-0 transition group-hover/row:opacity-100 text-text-muted hover:text-danger">
                      <Trash2 size={13} aria-hidden />
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-1 flex items-center gap-2">
        <button type="button" onClick={() => e.addRow(bi)}
          className="inline-flex items-center gap-1 rounded-md border border-border-subtle bg-surface-1 px-2 py-0.5 text-2xs font-medium text-text-secondary transition hover:border-accent hover:text-accent">
          <Plus size={12} aria-hidden /> صف
        </button>
        <button type="button" onClick={() => e.addCol(bi)}
          className="inline-flex items-center gap-1 rounded-md border border-border-subtle bg-surface-1 px-2 py-0.5 text-2xs font-medium text-text-secondary transition hover:border-accent hover:text-accent">
          <Plus size={12} aria-hidden /> عمود
        </button>
        {ncols > 1 && (
          <button type="button" onClick={() => e.delCol(bi, ncols - 1)}
            className="inline-flex items-center gap-1 rounded-md border border-border-subtle bg-surface-1 px-2 py-0.5 text-2xs font-medium text-text-muted transition hover:border-danger hover:text-danger">
            <Trash2 size={12} aria-hidden /> عمود
          </button>
        )}
      </div>
    </div>
  );
}

// Inside a table/grid cell, any `[…]` segment is a fill-in blank — whether it is the whole cell
// ("[1-5 / rating]") or embedded ("[Period end] — [Period start]"). Its inner text is the guidance. A
// clause cross-reference ([coso-1]) or the author's [PLACEHOLDERS] instruction is NOT a blank and stays
// literal. A blank renders as the SAME orange as an unfilled {{TOKEN}} — always, so a read-only view still
// shows what is unfilled — and becomes clickable (opening the editor) when editing is offered.
const FILL_BRACKET_G = /\[[^[\]]+\]/g;
const CLAUSE_REF = /^(coso|pdpl|ecc|imtithalak|otcc|iso|nca|sama)[-a-z0-9]*$/i;
function isFillBlank(inner: string): boolean {
  const t = inner.trim();
  return !!t && !CLAUSE_REF.test(t) && t.toUpperCase() !== 'PLACEHOLDERS';
}

function CellText({ text, ctx }: { text: string; ctx: Ctx }): ReactNode {
  // A pure clause / number cell ("1-1-2", "13–15", "2.5"): force LTR + no-wrap + tabular figures. In an
  // RTL table the neutral hyphens let the bidi algorithm REORDER the digit runs, so "1-1-2" displayed as
  // "2-1-1", and a narrow column wrapped the number over two lines. dir=ltr pins the reading order.
  const trimmed = (text || '').trim();
  if (trimmed && /^[0-9][0-9.–—\-\s/]*$/.test(trimmed)) {
    return <span dir="ltr" className="inline-block whitespace-nowrap tabular-nums">{text}</span>;
  }
  const parts: ReactNode[] = [];
  let last = 0; let i = 0; let m: RegExpExecArray | null;
  FILL_BRACKET_G.lastIndex = 0;
  while ((m = FILL_BRACKET_G.exec(text)) !== null) {
    const inner = m[0].slice(1, -1).trim();
    if (!isFillBlank(inner)) continue;   // clause ref / instruction → leave it in the plain flow
    if (m.index > last) parts.push(<Tokens key={`c${i}`} text={text.slice(last, m.index)} ctx={ctx} />);
    parts.push(
      ctx.onEditCell ? (
        <button key={`f${i}`} type="button" onClick={ctx.onEditCell} title={ctx.editLabel ?? ctx.unfilled}
          className="rounded bg-warning/15 px-1 py-0.5 text-[0.9em] font-medium text-warning underline decoration-dotted underline-offset-2 transition hover:bg-warning/25">
          {inner}
        </button>
      ) : (
        <mark key={`f${i}`} title={ctx.unfilled}
          className="rounded bg-warning/15 px-1 py-0.5 text-[0.9em] font-medium text-warning">{inner}</mark>
      ),
    );
    last = m.index + m[0].length; i += 1;
  }
  if (parts.length === 0) return <Tokens text={text} ctx={ctx} />;
  if (last < text.length) parts.push(<Tokens key={`c${i}`} text={text.slice(last)} ctx={ctx} />);
  return <>{parts}</>;
}

// A bilingual Text node → the reading edition, falling back to the other so a half-authored block is
// never blank.
function pick(t: unknown, locale: Locale): string {
  if (t && typeof t === 'object') {
    const o = t as { en?: string; ar?: string };
    return (locale === 'ar' ? o.ar : o.en) || o.en || o.ar || '';
  }
  return typeof t === 'string' ? t : '';
}

function humanise(key: string): string {
  return key.replace(/_/g, ' ').toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
}

// Bullet / reference heads are authored as labels, and MANY already carry their own trailing em-dash
// ("الغرض —", "ميثاق السلوك —"). The renderer also joins head→body with " — ", which doubled the dash.
// Emit our separator only when the head does NOT already end in one — otherwise a single space, so the
// authored dash stands as the separator and we never render "— —".
const ENDS_IN_DASH = /[—–-]\s*$/;
const headSep = (head: string, body: string): string => (head && body ? (ENDS_IN_DASH.test(head) ? ' ' : ' — ') : '');

const PH = /\{\{([A-Z0-9_]+)\}\}/g;

/** Render one authored string, resolving `{{KEY}}` placeholders. Everything is a React text node, so
 *  React escapes it — there is no path to innerHTML. */
function Tokens({ text, ctx }: { text: string; ctx: Ctx }): ReactNode {
  if (!text) return null;
  const out: ReactNode[] = [];
  let last = 0; let m: RegExpExecArray | null; let i = 0;
  PH.lastIndex = 0;
  while ((m = PH.exec(text)) !== null) {
    if (m.index > last) out.push(<span key={`t${i}`}>{inlineMarks(text.slice(last, m.index), `t${i}`)}</span>);
    const key = m[1];
    const val = ctx.resolve?.(key);
    const readonly = ctx.readonlyKeys?.has(key);
    if (ctx.onEdit && !readonly) {
      // #953 — TWO colours so filled vs unfilled reads at a glance: a FILLED value is GREEN (a value the
      // customer entered — click to change it); an UNFILLED one is the brighter ORANGE warning (click to
      // fill it). Either way the editable spots never blend into the prose.
      out.push(
        <button key={`e${i}`} type="button" onClick={() => ctx.onEdit!(key)} title={ctx.editLabel ?? ctx.unfilled}
          className={val
            ? 'rounded bg-success/10 px-1 py-0.5 font-medium text-success underline decoration-dotted decoration-success/60 underline-offset-2 transition hover:bg-success/20'
            : 'rounded bg-warning/15 px-1 py-0.5 text-[0.9em] font-medium text-warning underline decoration-dotted underline-offset-2 transition hover:bg-warning/25'}>
          {val || ctx.labelOf?.(key) || humanise(key)}
        </button>);
    } else if (readonly && val) {
      // Platform-SET (KD-45): shown, but clearly AUTO — a faint dotted underline + tooltip so it never
      // reads as a hardcoded value. It is not editable here (the effective-date picker controls it).
      out.push(
        <span key={`r${i}`} title={ctx.autoLabel} className="font-medium text-text-primary underline decoration-dotted decoration-text-muted/50 underline-offset-2">{val}</span>);
    } else if (val) {
      out.push(<span key={`v${i}`} className="font-medium text-text-primary">{val}</span>);
    } else {
      out.push(
        <mark key={`p${i}`} title={ctx.unfilled}
          className="rounded bg-warning/15 px-1 py-0.5 text-[0.9em] font-medium text-warning">
          {ctx.labelOf?.(key) || humanise(key)}
        </mark>);
    }
    last = m.index + m[0].length; i += 1;
  }
  if (last < text.length) out.push(<span key={`t${i}`}>{inlineMarks(text.slice(last), `t${i}`)}</span>);
  return <>{out}</>;
}

/** A block of prose text — blank lines split paragraphs, single newlines break lines. */
function Prose({ text, ctx }: { text: string; ctx: Ctx }): ReactNode {
  return (
    <>
      {text.split(/\n{2,}/).map((para, i) => (
        <p key={i}>
          {para.split('\n').map((ln, j) => (
            <span key={j}>{j > 0 && <br />}<Tokens text={ln} ctx={ctx} /></span>
          ))}
        </p>
      ))}
    </>
  );
}

// ── #844 rich free-text editing: any prose field becomes a Word-like editable region ────────────────
function escHtml(s: string): string {
  return s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c] as string));
}
// The editable field's innerHTML: plain text is freely editable; each {{TOKEN}} is an INERT chip (click
// to fill) carrying its raw `{{KEY}}` so editing the prose around it never drops or corrupts the token.
function richHtml(text: string, ctx: Ctx): string {
  let html = ''; let last = 0; let m: RegExpExecArray | null;
  PH.lastIndex = 0;
  while ((m = PH.exec(text)) !== null) {
    if (m.index > last) html += escHtml(text.slice(last, m.index));
    const key = m[1]; const val = ctx.resolve?.(key);
    if (ctx.readonlyKeys?.has(key)) {
      // Platform-SET (KD-45: EFFECTIVE_DATE, VERSION). Even in edit mode these are NOT fillable blanks —
      // render the live value, never an orange chip. Keep the raw `{{KEY}}` in data-raw so saving preserves
      // the token (not the literal value), and omit data-token so a click does nothing.
      html += `<span contenteditable="false" data-raw="${escHtml(m[0])}" class="rich-auto" title="${escHtml(ctx.autoLabel ?? '')}">${escHtml(val || ctx.labelOf?.(key) || humanise(key))}</span>`;
    } else {
      const cls = val ? 'rich-chip rich-chip-filled' : 'rich-chip rich-chip-empty';
      html += `<span contenteditable="false" data-token="${escHtml(key)}" data-raw="${escHtml(m[0])}" class="${cls}">${escHtml(val || ctx.labelOf?.(key) || humanise(key))}</span>`;
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) html += escHtml(text.slice(last));
  return html || '<br>';
}
// Read the field back OUT of the DOM: text nodes are literal; a chip contributes its raw `{{KEY}}`.
function richText(el: HTMLElement): string {
  let out = '';
  el.childNodes.forEach((n) => {
    if (n.nodeType === Node.TEXT_NODE) out += n.nodeValue ?? '';
    else if (n.nodeType === Node.ELEMENT_NODE) {
      const raw = (n as HTMLElement).getAttribute('data-raw');
      out += raw != null ? raw : ((n as HTMLElement).textContent ?? '');
    }
  });
  return out;
}
// One editable prose field. Uncontrolled while typing (the caret never jumps); commits on blur. Clicking
// a token chip opens its fill. `data-src` guards the effect so external updates re-sync but typing does not.
function EditableRich({ value, path, ctx, tag = 'span' }: {
  value: string; path: (string | number)[]; ctx: Ctx; tag?: 'span' | 'p' | 'div';
}): ReactNode {
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (el && el.getAttribute('data-src') !== value) { el.innerHTML = richHtml(value, ctx); el.setAttribute('data-src', value); }
  });
  const commit = () => {
    const el = ref.current; if (!el) return;
    const t = richText(el);
    if (t !== value) { el.setAttribute('data-src', t); ctx.edit!.onText(ctx.blockIndex ?? 0, path, t); }
  };
  const onClick = (e: { target: EventTarget | null; preventDefault: () => void }) => {
    const chip = (e.target as HTMLElement).closest?.('[data-token]');
    if (chip && ctx.onEdit) { e.preventDefault(); ctx.onEdit(chip.getAttribute('data-token')!); }
  };
  // An EMPTY field (e.g. a just-inserted block) would collapse to a bare caret and look like "nothing
  // happened". Give it a visible min-size + a soft tint + dashed ring so it reads as an editable slot the
  // author can click straight into. The cue clears the moment there is content.
  const empty = !value || !value.trim();
  return createElement(tag, {
    ref, contentEditable: true, suppressContentEditableWarning: true,
    dir: ctx.locale === 'ar' ? 'rtl' : 'ltr', onBlur: commit, onClick,
    className: `rich-edit rounded-sm px-1 outline-none transition hover:bg-[color:var(--accent)]/[0.04] focus:bg-[color:var(--accent)]/[0.06] ${
      empty ? 'inline-block min-h-[1.6em] min-w-[8ch] bg-[color:var(--accent)]/[0.05] ring-1 ring-inset ring-[color:var(--accent)]/25' : ''}`,
  });
}
// Switch: an editable field in quick-edit mode, else the read renderer (CellText handles both {{TOKEN}}
// resolution and [fill-in] chips, so a read-mode blank still lights up and click-to-edit still works).
function T({ v, path, ctx, tag }: { v: string; path: (string | number)[]; ctx: Ctx; tag?: 'span' | 'p' | 'div' }): ReactNode {
  return ctx.edit ? <EditableRich value={v} path={path} ctx={ctx} tag={tag} /> : <CellText text={v} ctx={ctx} />;
}

const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);

// ── the registry ────────────────────────────────────────────────────────────────────────────────────
type Renderer = (a: Record<string, unknown>, ctx: Ctx) => ReactNode;

const RENDERERS: Record<string, Renderer> = {
  cover: (a, ctx) => (
    <header className="mb-6 border-b border-border-subtle pb-5 text-center">
      <h1 className="text-2xl font-bold leading-tight text-text-primary"><T v={pick(a.title, ctx.locale)} path={['title']} ctx={ctx} /></h1>
      {(pick(a.subtitle, ctx.locale) || ctx.edit) && (
        <p className="mt-2 text-sm text-text-muted"><T v={pick(a.subtitle, ctx.locale)} path={['subtitle']} ctx={ctx} /></p>
      )}
    </header>
  ),

  section: (a, ctx) => {
    const lvl = Math.min(5, Math.max(2, Number(a.level) || 2));
    const Tag = (`h${lvl}` as 'h2');
    return <Tag><T v={pick(a.text, ctx.locale)} path={['text']} ctx={ctx} /></Tag>;
  },

  lead: (a, ctx) => (ctx.edit
    ? <div className="my-2 leading-relaxed"><T v={pick(a.text, ctx.locale)} path={['text']} ctx={ctx} tag="div" /></div>
    : <Prose text={pick(a.text, ctx.locale)} ctx={ctx} />),
  flag: (a, ctx) => (
    <div className="legal-callout legal-callout-warn">
      {ctx.edit ? <T v={pick(a.text, ctx.locale)} path={['text']} ctx={ctx} tag="div" /> : <Prose text={pick(a.text, ctx.locale)} ctx={ctx} />}
    </div>
  ),
  helper: (a, ctx) => (
    <p className="text-xs italic text-text-muted"><T v={pick(a.text, ctx.locale)} path={['text']} ctx={ctx} /></p>
  ),
  footer_band: (a, ctx) => (
    <p className="mt-4 border-t border-border-subtle pt-3 text-center text-xs text-text-muted">
      <T v={pick(a.text, ctx.locale)} path={['text']} ctx={ctx} />
    </p>
  ),

  bullets: (a, ctx) => (
    <ul>
      {arr(a.items).map((pair, i) => {
        const [head, body] = Array.isArray(pair) ? pair : [pair, ''];
        return (
          <li key={i}>
            {(pick(head, ctx.locale) || ctx.edit) && <strong><T v={pick(head, ctx.locale)} path={['items', i, 0]} ctx={ctx} /></strong>}
            {headSep(pick(head, ctx.locale), pick(body, ctx.locale))}
            <T v={pick(body, ctx.locale)} path={['items', i, 1]} ctx={ctx} />
          </li>
        );
      })}
    </ul>
  ),

  clause: (a, ctx) => (
    <section className="mb-3">
      <h3>
        {a.n !== undefined && a.n !== null && a.n !== '' && (
          <span className="me-2 font-mono text-accent">{String(a.n)}</span>
        )}
        <T v={pick(a.title, ctx.locale)} path={['title']} ctx={ctx} />
      </h3>
      {arr(a.paras).map((p, i) => <p key={i}><T v={pick(p, ctx.locale)} path={['paras', i]} ctx={ctx} /></p>)}
    </section>
  ),

  table: (a, ctx) => {
    const headers = arr(a.headers);
    const rows = arr(a.rows);
    if (ctx.edit && ctx.blockIndex != null) {
      // Combined grid for editing: row 0 = headers (if any), then the data rows. The host maps indices
      // back to the block's `headers`/`rows` fields.
      const grid = headers.length ? [headers, ...rows] : rows;
      return <EditableGrid rows={grid} ctx={ctx} headerRow={headers.length > 0} />;
    }
    return (
      <div className="legal-table-wrap">
        <table className="legal-table">
          {headers.length > 0 && (
            <thead><tr>{headers.map((h, i) => <th key={i}><Tokens text={pick(h, ctx.locale)} ctx={ctx} /></th>)}</tr></thead>
          )}
          <tbody>
            {rows.map((row, ri) => (
              <tr key={ri}>{arr(row).map((c, ci) => <td key={ci}><CellText text={pick(c, ctx.locale)} ctx={ctx} /></td>)}</tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  },

  // The evidence register: a headerless grid of bilingual cells.
  evidence: (a, ctx) => (
    ctx.edit && ctx.blockIndex != null
      ? <EditableGrid rows={arr(a.rows)} ctx={ctx} headerRow={false} />
      : <div className="legal-table-wrap">
      <table className="legal-table">
        <tbody>
          {arr(a.rows).map((row, ri) => (
            <tr key={ri}>{arr(row).map((c, ci) => <td key={ci}><CellText text={pick(c, ctx.locale)} ctx={ctx} /></td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  ),

  callout: (a, ctx) => {
    const kind = typeof a.kind === 'string' ? a.kind : 'info';
    const tone = kind === 'warn' || kind === 'warning' ? 'legal-callout-warn'
      : kind === 'danger' ? 'legal-callout-danger' : kind === 'success' ? 'legal-callout-success' : '';
    return (
      <div className={`legal-callout ${tone}`}>
        {(pick(a.title, ctx.locale) || ctx.edit) && <p className="font-semibold text-text-primary"><T v={pick(a.title, ctx.locale)} path={['title']} ctx={ctx} /></p>}
        <ul>{arr(a.items).map((it, i) => <li key={i}><T v={pick(it, ctx.locale)} path={['items', i]} ctx={ctx} /></li>)}</ul>
      </div>
    );
  },

  // A snapshot / fields block: a label→value definition grid ([[label, value], …]).
  snapshot: (a, ctx) => <PairGrid rows={arr(a.rows)} ctx={ctx} />,
  fields: (a, ctx) => <PairGrid rows={arr(a.rows)} ctx={ctx} />,
  references: (a, ctx) => (
    <section className="mb-3">
      {(pick(a.title, ctx.locale) || ctx.edit) && <h3><T v={pick(a.title, ctx.locale)} path={['title']} ctx={ctx} /></h3>}
      <ul>
        {arr(a.items).map((pair, i) => {
          const [head, body] = Array.isArray(pair) ? pair : [pair, ''];
          return <li key={i}><strong><T v={pick(head, ctx.locale)} path={['items', i, 0]} ctx={ctx} /></strong>{headSep(pick(head, ctx.locale), pick(body, ctx.locale))}<T v={pick(body, ctx.locale)} path={['items', i, 1]} ctx={ctx} /></li>;
        })}
      </ul>
    </section>
  ),

  signoff: (a, ctx) => (
    <div className="mt-4 flex flex-wrap gap-8">
      {arr(a.roles).map((r, i) => (
        <div key={i} className="min-w-[10rem]">
          <div className="mt-6 border-t border-border-strong pt-1 text-xs text-text-muted"><T v={pick(r, ctx.locale)} path={['roles', i]} ctx={ctx} /></div>
        </div>
      ))}
    </div>
  ),

  metric: (a, ctx) => (
    <p className="flex items-baseline justify-between gap-3 border-b border-border-subtle py-1">
      <span className="text-text-secondary"><Tokens text={pick(a.label, ctx.locale)} ctx={ctx} /></span>
      <span className="font-medium tabular-nums text-text-primary">
        <Tokens text={pick(a.value, ctx.locale)} ctx={ctx} /> {pick(a.unit, ctx.locale)}
      </span>
    </p>
  ),
  metric_table: (a, ctx) => (
    <section className="mb-3">
      {pick(a.title, ctx.locale) && <h3><Tokens text={pick(a.title, ctx.locale)} ctx={ctx} /></h3>}
      <div className="legal-table-wrap">
        <table className="legal-table">
          <tbody>
            {arr(a.rows).map((r, i) => {
              const row = (r ?? {}) as Record<string, unknown>;
              return (
                <tr key={i}>
                  <td><Tokens text={pick(row.label, ctx.locale)} ctx={ctx} /></td>
                  <td className="tabular-nums"><CellText text={pick(row.value, ctx.locale)} ctx={ctx} /></td>
                  <td>{pick(row.unit, ctx.locale)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  ),

  page_break: () => <hr className="legal-divider" />,
};

function PairGrid({ rows, ctx }: { rows: unknown[]; ctx: Ctx }): ReactNode {
  return (
    <dl className="my-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
      {rows.map((pair, i) => {
        const [label, value] = Array.isArray(pair) ? pair : [pair, ''];
        return (
          <div key={i} className="contents">
            <dt className="font-medium text-text-secondary"><T v={pick(label, ctx.locale)} path={['rows', i, 0]} ctx={ctx} /></dt>
            <dd className="text-text-primary"><T v={pick(value, ctx.locale)} path={['rows', i, 1]} ctx={ctx} /></dd>
          </div>
        );
      })}
    </dl>
  );
}

// A block kind we do not yet render degrades to its text-bearing fields (forward-compatible, like
// BlockRenderer's null fallback but never fully blank if the block carries readable prose).
function Fallback({ args, ctx }: { args: Record<string, unknown>; ctx: Ctx }): ReactNode {
  const texts = Object.values(args).map((v) => pick(v, ctx.locale)).filter(Boolean);
  if (!texts.length) return null;
  return <>{texts.map((t, i) => <p key={i}><Tokens text={t} ctx={ctx} /></p>)}</>;
}

/** Render one documents-service block node. Unknown kinds degrade gracefully (forward-compatible). */
export function DocBlockView({ block, locale, resolve, unfilledLabel, labelOf, onEditPlaceholder, onEditCell, editLabel, readonlyKeys, autoLabel, edit, blockIndex }: {
  block: DocBlock; locale: Locale; resolve?: ResolveFn; unfilledLabel: string; labelOf?: (key: string) => string | undefined;
  onEditPlaceholder?: (key: string) => void; onEditCell?: () => void; editLabel?: string; readonlyKeys?: Set<string>; autoLabel?: string;
  edit?: EditOps; blockIndex?: number;
}): ReactNode {
  const kind = Object.keys(block)[0];
  if (!kind) return null;
  const args = (block[kind] ?? {}) as Record<string, unknown>;
  // Legacy legal block (#892): delegate to the legal renderer (markdown prose), reconstructing its
  // `{type,data}` shape. This is how the ONE renderer stays a superset of both families.
  const legalBase = legalKindBase(kind);
  if (legalBase) return <BlockView block={{ type: legalBase, data: args }} locale={locale} />;
  const ctx: Ctx = { locale, resolve, unfilled: unfilledLabel, labelOf, onEdit: onEditPlaceholder, onEditCell, editLabel, readonlyKeys, autoLabel, edit, blockIndex };
  const r = RENDERERS[kind];
  return <>{r ? r(args, ctx) : <Fallback args={args} ctx={ctx} />}</>;
}

/** The full document — every block, wrapped as legal-grade prose in the reading direction. When
 *  `onEditPlaceholder` is given, every {{PLACEHOLDER}} becomes a clickable chip (fill it inline). */
// #942 — the on-page "+" insert gap: a thin hover strip that reveals a palette of block kinds. Inserting
// here happens IN PLACE on the document, never in a separate editor. RTL/LTR agnostic.
function InsertGap({ at, chrome, onInsert }: { at: number; chrome: BlockChrome; onInsert: (at: number, kind: string) => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="not-prose relative flex h-3 items-center justify-center">
      <button type="button" aria-label={chrome.addLabel} title={chrome.addLabel} onClick={() => setOpen((v) => !v)}
        className={`z-10 grid h-5 w-5 place-items-center rounded-full border border-[color:var(--accent)]/40 bg-white text-[color:var(--accent)] shadow-sm transition group-hover/doc:opacity-100 hover:bg-[color:var(--accent)] hover:text-white focus:opacity-100 ${open ? 'bg-[color:var(--accent)] text-white opacity-100' : 'opacity-0'}`}>
        <Plus size={12} aria-hidden />
      </button>
      <span aria-hidden className="pointer-events-none absolute inset-x-8 top-1/2 h-px -translate-y-1/2 bg-[color:var(--accent)]/20 opacity-0 group-hover/doc:opacity-100" />
      {open && (
        <>
          {/* click-away backdrop — closes only on an OUTSIDE click, so moving the mouse onto the menu is safe */}
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} aria-hidden />
          <div className="absolute top-6 z-20 flex flex-col gap-0.5 rounded-xl border border-border-subtle bg-surface-1 p-1.5 shadow-pop">
            {chrome.types.map((tp) => (
              <button key={tp.kind} type="button" onClick={() => { onInsert(at, tp.kind); setOpen(false); }}
                className="whitespace-nowrap rounded-lg px-3 py-1.5 text-start text-sm text-text-secondary transition hover:bg-accent-soft hover:text-accent">
                {tp.label}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

export function DocBlocksView({ blocks, locale, resolve, unfilledLabel, labelOf, onEditPlaceholder, onEditCell, editLabel, readonlyKeys, autoLabel, edit, chrome }: {
  blocks: DocBlock[]; locale: Locale; resolve?: ResolveFn; unfilledLabel: string; labelOf?: (key: string) => string | undefined;
  onEditPlaceholder?: (key: string) => void; onEditCell?: () => void; editLabel?: string; readonlyKeys?: Set<string>; autoLabel?: string;
  edit?: EditOps;
  // #942 — present (with edit.moveBlock/insertBlock/removeBlock) ⇒ the page becomes a Word-like block
  // author: drag handles, per-block delete, and "+" insert gaps. Absent ⇒ the plain read/inline-edit page.
  chrome?: BlockChrome;
}): ReactNode {
  const authoring = !!(chrome && edit && (edit.moveBlock || edit.insertBlock || edit.removeBlock));
  const dragFrom = useRef<number | null>(null);
  const [dragOver, setDragOver] = useState<number | null>(null);

  if (!authoring) {
    return (
      <article className="legal-prose" dir={locale === 'ar' ? 'rtl' : 'ltr'}>
        {blocks.map((b, i) => (
          <DocBlockView key={i} block={b} locale={locale} resolve={resolve} unfilledLabel={unfilledLabel} labelOf={labelOf}
            onEditPlaceholder={onEditPlaceholder} onEditCell={onEditCell} editLabel={editLabel} readonlyKeys={readonlyKeys} autoLabel={autoLabel} edit={edit} blockIndex={i} />
        ))}
      </article>
    );
  }

  const c = chrome!;
  return (
    <article className="legal-prose group/doc" dir={locale === 'ar' ? 'rtl' : 'ltr'}>
      {edit!.insertBlock && <InsertGap at={0} chrome={c} onInsert={edit!.insertBlock} />}
      {blocks.map((b, i) => (
        <div key={i}>
          <div
            onDragOver={(e) => { if (dragFrom.current !== null) { e.preventDefault(); if (dragOver !== i) setDragOver(i); } }}
            onDrop={(e) => { e.preventDefault(); if (dragFrom.current !== null && edit!.moveBlock) edit!.moveBlock(dragFrom.current, i); dragFrom.current = null; setDragOver(null); }}
            className={`not-prose group/blk relative rounded-lg border border-transparent px-8 transition hover:border-border-subtle hover:bg-surface-2/20 ${dragOver === i && dragFrom.current !== i ? 'ring-2 ring-accent/50' : ''}`}
          >
            {/* the block chrome — a drag handle + delete, tucked on the leading edge, shown on hover */}
            <div className="absolute inset-y-0 start-0 flex flex-col items-center justify-start gap-1 pt-3 opacity-0 transition group-hover/blk:opacity-100">
              {edit!.moveBlock && (
                <span role="button" tabIndex={0} aria-label={c.dragLabel} title={c.dragLabel} draggable
                  onDragStart={(e) => { dragFrom.current = i; e.dataTransfer.effectAllowed = 'move'; }}
                  onDragEnd={() => { dragFrom.current = null; setDragOver(null); }}
                  className="grid h-6 w-5 cursor-grab place-items-center rounded text-text-muted hover:bg-surface-2 hover:text-text-secondary active:cursor-grabbing">
                  <GripVertical size={14} aria-hidden />
                </span>
              )}
              {edit!.removeBlock && (
                <button type="button" aria-label={c.deleteLabel} title={c.deleteLabel} onClick={() => edit!.removeBlock!(i)}
                  className="grid h-6 w-5 place-items-center rounded text-text-muted hover:bg-danger-soft hover:text-danger">
                  <Trash2 size={13} aria-hidden />
                </button>
              )}
            </div>
            <DocBlockView block={b} locale={locale} resolve={resolve} unfilledLabel={unfilledLabel} labelOf={labelOf}
              onEditPlaceholder={onEditPlaceholder} onEditCell={onEditCell} editLabel={editLabel} readonlyKeys={readonlyKeys} autoLabel={autoLabel} edit={edit} blockIndex={i} />
          </div>
          {edit!.insertBlock && <InsertGap at={i + 1} chrome={c} onInsert={edit!.insertBlock} />}
        </div>
      ))}
    </article>
  );
}

/** The block kinds this client can render — mirrors BlockRenderer.KNOWN_BLOCK_TYPES. */
export const KNOWN_DOC_BLOCK_KINDS = Object.keys(RENDERERS);
