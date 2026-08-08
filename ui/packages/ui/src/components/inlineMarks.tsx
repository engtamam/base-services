// A deliberately tiny inline formatter, shared by BOTH block renderers (BlockRenderer for the legal
// `{type,data}` model, DocBlockRenderer for the documents `{kind}` model). Adds **bold**, *italic*,
// `code`, and [text](https://…) links only, all as React text/elements — so React does the escaping and
// no markup can be injected. No block HTML, no attributes beyond a vetted href. There is no path from
// document data to innerHTML, so a stray '<script>' is inert text.
import type { ReactNode } from 'react';

export function inlineMarks(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  // NB: NO HTML-escaping here. Everything below is emitted as React TEXT nodes, which React escapes on
  // its own — pre-escaping would turn a real '"' into the literal '&quot;' on screen.
  const safe = text;
  // Split on the marks we support; process links last so their inner text can still be bold/italic.
  const linkRe = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  const emitPlain = (chunk: string, kb: string) => {
    // **bold**, *italic*, `code`
    const parts = chunk.split(/(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)/g);
    parts.forEach((p, j) => {
      if (!p) return;
      if (p.startsWith('**') && p.endsWith('**')) out.push(<strong key={`${kb}-b${j}`}>{p.slice(2, -2)}</strong>);
      else if (p.startsWith('`') && p.endsWith('`')) out.push(<code key={`${kb}-c${j}`}>{p.slice(1, -1)}</code>);
      else if (p.startsWith('*') && p.endsWith('*')) out.push(<em key={`${kb}-i${j}`}>{p.slice(1, -1)}</em>);
      else out.push(<span key={`${kb}-t${j}`}>{p}</span>);
    });
  };
  while ((m = linkRe.exec(safe)) !== null) {
    if (m.index > last) emitPlain(safe.slice(last, m.index), `${keyBase}-${i}`);
    out.push(
      <a key={`${keyBase}-l${i}`} href={m[2]} target="_blank" rel="noopener noreferrer nofollow">
        {m[1]}
      </a>,
    );
    last = m.index + m[0].length;
    i += 1;
  }
  if (last < safe.length) emitPlain(safe.slice(last), `${keyBase}-${i}`);
  return out;
}
