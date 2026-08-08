import { createContext, useContext, useMemo, type ReactNode } from 'react';

// ── The ShellSlot — the ONE seam the shared design system needs from its host app ────────────────
// A handful of design-system components (Pager, Segmented, Tabs) need two things that are genuinely
// channel-specific: (1) a translator `t` for their built-in labels, and (2) how to render the app's
// contextual-help "?" control. Rather than import a concrete app shell (`@/app/...`) — which would
// couple the shared lib to one channel — the host injects them ONCE at the root via <ImtithalakUiProvider>.
// On-prem wires its `useShell().t` + its <HelpIcon/>; Cloud can wire its own (or nothing). Components
// read them through `useImtithalakUi()`. This keeps the shared lib app-agnostic while preserving byte-for-byte
// output on-prem (same `t` values, same <HelpIcon/> element).

export interface HelpRenderProps {
  /** Knowledge-base key, `<section>.<element>` (e.g. `findings.tier`). */
  entryId: string;
  className?: string;
}
export type HelpRenderer = (props: HelpRenderProps) => ReactNode;

export interface ImtithalakUiShell {
  /** Translate a shared-component label key. Defaults to identity (returns the key unchanged). */
  t: (key: string) => string;
  /** Render the host app's contextual-help control for a kb key. Undefined → no help icon shown. */
  renderHelp?: HelpRenderer;
}

const defaultShell: ImtithalakUiShell = { t: (k) => k };

const ImtithalakUiContext = createContext<ImtithalakUiShell>(defaultShell);

export function ImtithalakUiProvider({
  t,
  renderHelp,
  children,
}: ImtithalakUiShell & { children: ReactNode }) {
  const value = useMemo<ImtithalakUiShell>(() => ({ t, renderHelp }), [t, renderHelp]);
  return <ImtithalakUiContext.Provider value={value}>{children}</ImtithalakUiContext.Provider>;
}

/** Read the injected shell seam (translator + help renderer). Safe to call without a provider —
 *  falls back to an identity translator and no help control. */
export function useImtithalakUi(): ImtithalakUiShell {
  return useContext(ImtithalakUiContext);
}
