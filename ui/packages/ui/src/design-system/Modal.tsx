import {
  createContext, useContext, useEffect, useRef, useSyncExternalStore, type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';

// SHARED. This lived twice — once in the client, once (a simpler copy) in the admin console, which is
// how the admin ended up without the stacking register below and therefore with an Escape that closed
// two dialogs at once. One implementation, both apps.
//
// A centered modal dialog — for the app's primary, focus-demanding flows (the Answer Wizard). Backdrop
// scrim + a raised card, escape-to-close, click-outside-to-close, body-scroll lock, and the page-reveal
// motion. RTL-safe (logical spacing). Prefer this over the side Drawer for main-product interactions.
const SIZE = {
  md: 'max-w-lg',      // the default: a focused prompt (confirm, one form)
  lg: 'max-w-3xl',
  xl: 'max-w-5xl',     // a detail surface that carries tabs + lists (a standard and its policies)
  '2xl': 'max-w-6xl',  // a full hub with a side rail (the Account settings surface)
  '3xl': 'max-w-[88rem]', // the widest — a document workspace: index rail + a roomy editing pane
} as const;

// ── ONLY THE TOP MODAL IS ON SCREEN ──────────────────────────────────────────────────────────────
//
// A dialog opened FROM a dialog (Generate, from inside a standard) used to leave both mounted. The
// upper card never covered the lower one exactly, so the customer saw the parent's edge as a grey
// frame around the child and read it — correctly — as broken. Darkening the scrim only hid it better:
// at 80% the white card underneath still showed through as grey.
//
// So the stack, not the scrim, is the fix: a modal that is not on top renders nothing. Two things make
// that safe. Each modal PORTALS to document.body, so the child is a sibling of the parent rather than
// its descendant — hiding the parent cannot unmount the child that lives inside it. And only the top
// modal binds Escape, so one press closes one dialog, the one you are looking at.
// ORDERED BY TREE DEPTH, NOT BY EFFECT ORDER. Registering on mount looked right and was backwards:
// React runs a CHILD's effects before its parent's, so the dialog opened on top registered FIRST and
// the parent came out "topmost" — Escape closed the wrong dialog and the wrong card was hidden.
// Depth comes from the React tree itself, so it cannot disagree with what is actually nested inside
// what.
const ModalDepth = createContext(0);

const open_: Map<symbol, number> = new Map();          // id -> depth, for every OPEN modal
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());
const subscribe = (l: () => void) => { listeners.add(l); return () => { listeners.delete(l); }; };

let cachedTop: symbol | undefined;
// useSyncExternalStore compares snapshots by identity, so this must return the SAME symbol until the
// answer actually changes — recomputing a fresh value every call would re-render forever.
function topOfStack(): symbol | undefined {
  let best: symbol | undefined;
  let bestDepth = -1;
  // `>=`, not `>`, so a TIE at the same tree depth is broken by OPEN ORDER — the Map keeps insertion
  // order, so the sibling opened LAST wins. Depth still makes a truly-nested child (higher depth) win
  // outright; this only decides two modals at the same depth, e.g. a confirm opened as a sibling of the
  // detail sheet it was launched from. Without it the first sibling stayed "top" and the second rendered
  // display:none — the "it shows under the window / nothing happens" bug.
  for (const [id, depth] of open_) if (depth >= bestDepth) { best = id; bestDepth = depth; }
  if (best !== cachedTop) cachedTop = best;
  return cachedTop;
}

export function Modal({
  open,
  onClose,
  title,
  subtitle,
  size = 'md',
  tabs,
  toolbar,
  headerExtra,
  sidebar,
  tall,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  subtitle?: ReactNode;
  size?: keyof typeof SIZE;
  // Fill the viewport height (a workspace, not a prompt) instead of hugging the content — so a short
  // document still opens into a roomy editing surface rather than a cramped little card.
  tall?: boolean;
  tabs?: ReactNode;    // rendered under the header, above the scrolling body (the tab strip)
  // Pinned under the header and ABOVE the scrolling body: controls that must stay reachable however
  // far down the content you are (the edition switcher, the effective date). A control that scrolls
  // out of reach is a control the customer has to hunt for.
  toolbar?: ReactNode;
  // Rendered at the end of the header row, beside the close button — a status pill, say. The admin
  // console's copy grew this to keep a customer's state visible while acting on them.
  headerExtra?: ReactNode;
  // A FIXED left rail beside the scrolling content — the settings-hub layout. When set, the body
  // splits into an aside (the section nav, its own scroll) and the content pane (scrolls independently),
  // so the nav never scrolls away under a long section. Omit for the ordinary single-column body.
  sidebar?: ReactNode;
  children: ReactNode;
}) {
  // A stable identity for this modal's slot in the stack.
  const idRef = useRef<symbol | null>(null);
  if (idRef.current === null) idRef.current = Symbol('modal');
  const id = idRef.current;

  const depth = useContext(ModalDepth);

  useEffect(() => {
    if (!open) return;
    open_.set(id, depth);
    emit();
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      open_.delete(id);
      emit();
      document.body.style.overflow = prev;
    };
  }, [open, id, depth]);

  const isTop = useSyncExternalStore(subscribe, topOfStack) === id;

  useEffect(() => {
    // Escape belongs to the dialog he is LOOKING AT. Bound on every open modal it closed the whole
    // stack at once, so one press dismissed work he had not finished.
    if (!open || !isTop) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, isTop, onClose]);

  if (!open) return null;

  // HIDDEN, NOT UNMOUNTED — and that distinction is the whole thing working.
  //
  // Returning null when not on top sent React into an infinite loop: the child modal is rendered
  // INSIDE the parent's element tree, so dropping the parent unmounted the child, which popped it off
  // the stack, which made the parent top again, which remounted the child... "Maximum update depth
  // exceeded", every time. Portalling the DOM does not help: the React tree is still nested.
  //
  // `display:none` keeps the parent's subtree — and therefore the child — mounted and stateful, while
  // taking it off the screen AND out of the accessibility tree, so exactly one dialog is exposed.
  return createPortal(
    // An INLINE style, not a `hidden` utility class: this must hold whether or not a stylesheet has
    // loaded, and it must not depend on a utility surviving a CSS purge. Getting it wrong shows the
    // customer two stacked dialogs.
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={isTop ? undefined : { display: 'none' }}
      role="dialog" aria-modal="true" aria-hidden={!isTop || undefined}>
      <div className="absolute inset-0 bg-black/50 backdrop-blur-md" onClick={onClose} aria-hidden />
      <div className={`animate-in relative z-10 flex max-h-[92vh] w-full ${SIZE[size]} ${tall ? 'h-[92vh]' : ''} flex-col overflow-hidden rounded-card border border-border-subtle bg-surface-1 shadow-pop`}>
        <div className="flex items-start justify-between gap-4 border-b border-border-subtle p-5">
          <div>
            <h2 className="text-lg font-semibold tracking-tight text-text-primary">{title}</h2>
            {subtitle && <p className="mt-0.5 text-sm text-text-muted">{subtitle}</p>}
          </div>
          <div className="flex shrink-0 items-center gap-2">
          {headerExtra}
          <button
            type="button"
            onClick={onClose}
            className="rounded-md p-1.5 text-text-muted transition hover:bg-surface-2 hover:text-text-primary focus-ring"
            aria-label="Close"
          >
            <X size={18} />
          </button>
          </div>
        </div>
        {tabs && <div className="flex gap-1 border-b border-border-subtle px-5 pt-2">{tabs}</div>}
        {toolbar && (
          <div className="shrink-0 border-b border-border-subtle bg-surface-2/60 px-5 py-3">{toolbar}</div>
        )}
        {/* min-h-0 is REQUIRED: a flex child defaults to min-height:auto, which refuses to shrink below
            its content — so the body grew past the card instead of scrolling inside it, and a pinned
            toolbar above it would have been pushed out of view. */}
        {sidebar ? (
          <div className="flex min-h-0 flex-1">
            <aside className="w-56 shrink-0 overflow-y-auto border-e border-border-subtle bg-surface-2/40 p-3 sm:w-64">
              {sidebar}
            </aside>
            <div className="min-h-0 min-w-0 flex-1 overflow-y-auto p-6">
              <ModalDepth.Provider value={depth + 1}>{children}</ModalDepth.Provider>
            </div>
          </div>
        ) : (
          <div className="min-h-0 flex-1 overflow-y-auto p-5">
            <ModalDepth.Provider value={depth + 1}>{children}</ModalDepth.Provider>
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}
