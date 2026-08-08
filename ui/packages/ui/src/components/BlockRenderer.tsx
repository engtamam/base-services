// The block renderer registry — one renderer per block `type`, so the backend stays type-agnostic and
// a NEW block kind is one entry here, never a schema change. An unknown type degrades to nothing
// (forward-compatible: an older client simply skips a block a newer document introduced).
//
// SAFETY: block content is authored by our own operators (first-party), but we still render it as TEXT,
// never as raw HTML — `inline()` escapes everything and then re-introduces only a small, closed set of
// inline marks. There is no path from document data to innerHTML, so a stray '<script>' is inert text.
import type { ReactNode } from 'react';
import { blockAssetBase } from './blockAssets';
import { inlineMarks as inline } from './inlineMarks';

// Localized here so the shared renderer imports NO app-specific module (#896 renderer move). A legal block
// is the open `{ type, data }` shape; Locale is the product's two editions.
type Locale = 'en' | 'ar';
export type Block = { type: string; data: Record<string, unknown> };

function pick(data: Record<string, unknown>, base: string, locale: Locale): string {
  const v = data[`${base}_${locale}`] ?? data[`${base}_en`] ?? data[base];
  return typeof v === 'string' ? v : '';
}

function pickArr(data: Record<string, unknown>, base: string, locale: Locale): unknown[] {
  const v = data[`${base}_${locale}`] ?? data[`${base}_en`] ?? data[base];
  return Array.isArray(v) ? v : [];
}

// Inline marks (**bold**, *italic*, `code`, links) come from the shared `inlineMarks` helper, used by
// this renderer AND DocBlockRenderer so both document models format prose identically.

// A clause cross-reference like "6-4" (section 6, sub-clause 4) is a hyphen-joined number. Inside an
// Arabic (RTL) paragraph the two number runs can be perceived — and, next to brackets/punctuation, laid
// out — in the wrong order, so "6-4" reads like "4-6". Wrapping each such reference in a directional
// ISOLATE (U+2066 LRI … U+2069 PDI) pins it to a single left-to-right run, so it always renders exactly
// as authored, in every document and both languages. Invisible; changes no content.
const _REF = /(\d+(?:[-–]\d+)+)/g;
function isolateRefs(text: string): string {
  return text.replace(_REF, '⁦$1⁩');
}

/** Render a markdown-ish string as a stack of paragraphs, headings and lists. Legal-grade, not a full
 *  markdown engine: `## heading`, `- bullet` / `1. ordered`, and blank-line-separated paragraphs. */
function Markdown({ text }: { text: string }): ReactNode {
  const lines = isolateRefs(text).replace(/\r\n/g, '\n').split('\n');
  const blocks: ReactNode[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let para: string[] = [];
  const flushPara = () => {
    if (para.length) { blocks.push(<p key={`p${blocks.length}`}>{inline(para.join(' '), `p${blocks.length}`)}</p>); para = []; }
  };
  const flushList = () => {
    if (list) {
      const items = list.items.map((it, k) => <li key={k}>{inline(it, `li${blocks.length}-${k}`)}</li>);
      blocks.push(list.ordered ? <ol key={`l${blocks.length}`}>{items}</ol> : <ul key={`l${blocks.length}`}>{items}</ul>);
      list = null;
    }
  };
  const cells = (row: string) => row.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map((c) => c.trim());
  const isRow = (s: string) => /^\s*\|.*\|\s*$/.test(s);
  const isSep = (s: string) => /^\s*\|[\s:|-]+\|\s*$/.test(s);
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trimEnd();
    // GFM table: a header row, a |---| separator, then body rows.
    if (isRow(line) && i + 1 < lines.length && isSep(lines[i + 1])) {
      flushPara(); flushList();
      const header = cells(line);
      const body: string[][] = [];
      i += 2;
      while (i < lines.length && isRow(lines[i])) { body.push(cells(lines[i])); i += 1; }
      i -= 1;
      const kb = `tb${blocks.length}`;
      blocks.push(
        <div key={kb} className="legal-table-wrap">
          <table className="legal-table">
            <thead><tr>{header.map((h, j) => <th key={j}>{inline(h, `${kb}h${j}`)}</th>)}</tr></thead>
            <tbody>{body.map((r, ri) => <tr key={ri}>{r.map((c, ci) => <td key={ci}>{inline(c, `${kb}${ri}-${ci}`)}</td>)}</tr>)}</tbody>
          </table>
        </div>,
      );
      continue;
    }
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    const bullet = /^[-*]\s+(.*)$/.exec(line);
    const ordered = /^\d+[.)]\s+(.*)$/.exec(line);
    if (heading) {
      flushPara(); flushList();
      const lvl = Math.min(4, heading[1].length);
      const Tag = (`h${lvl + 1}` as 'h2');
      blocks.push(<Tag key={`h${blocks.length}`}>{inline(heading[2], `h${blocks.length}`)}</Tag>);
    } else if (bullet || ordered) {
      flushPara();
      const wantOrdered = !!ordered;
      if (!list || list.ordered !== wantOrdered) { flushList(); list = { ordered: wantOrdered, items: [] }; }
      list.items.push((bullet ?? ordered)![1]);
    } else if (!line.trim()) {
      flushPara(); flushList();
    } else {
      flushList(); para.push(line.trim());
    }
  }
  flushPara(); flushList();
  return <>{blocks}</>;
}

// ── the registry ──────────────────────────────────────────────────────────────────────────────────
type Renderer = (block: Block, locale: Locale) => ReactNode;

const RENDERERS: Record<string, Renderer> = {
  text: (b, l) => <div className="legal-prose">{<Markdown text={pick(b.data, 'md', l)} />}</div>,

  callout: (b, l) => {
    const tone = (typeof b.data.tone === 'string' ? b.data.tone : 'info');
    return (
      <div className={`legal-callout legal-callout-${tone}`}>
        <Markdown text={pick(b.data, 'md', l)} />
      </div>
    );
  },

  table: (b, l) => {
    const headers = pickArr(b.data, 'headers', l).map(String);
    const rows = pickArr(b.data, 'rows', l);
    return (
      <div className="legal-table-wrap">
        <table className="legal-table">
          {headers.length > 0 && (
            <thead>
              <tr>{headers.map((h, i) => <th key={i}>{inline(h, `th${i}`)}</th>)}</tr>
            </thead>
          )}
          <tbody>
            {rows.map((row, ri) => (
              <tr key={ri}>
                {(Array.isArray(row) ? row : []).map((cell, ci) => (
                  <td key={ci}>{inline(String(cell), `td${ri}-${ci}`)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  },

  image: (b, l) => {
    const ref = typeof b.data.ref === 'string' ? b.data.ref : '';
    // A ref is a path in our own object store, served under the standards prefix; an absolute http(s)
    // url (a data: URI or a fully-qualified asset) is used as-is.
    const src = /^(https?:|data:)/.test(ref) ? ref : `${blockAssetBase()}${ref}`;
    const caption = pick(b.data, 'caption', l);
    const alt = typeof b.data.alt === 'string' ? b.data.alt : caption;
    return (
      <figure className="legal-figure">
        {ref && <img src={src} alt={alt} loading="lazy" style={{ maxWidth: '100%' }} />}
        {caption && <figcaption>{inline(caption, 'cap')}</figcaption>}
      </figure>
    );
  },

  divider: () => <hr className="legal-divider" />,
};

/** Render one block by its type. Unknown types render nothing (forward-compatible). */
export function BlockView({ block, locale }: { block: Block; locale: Locale }): ReactNode {
  const r = RENDERERS[block.type];
  return r ? <>{r(block, locale)}</> : null;
}

/** The set of types this client can currently render — the admin editor reads it to offer block kinds. */
export const KNOWN_BLOCK_TYPES = Object.keys(RENDERERS);
