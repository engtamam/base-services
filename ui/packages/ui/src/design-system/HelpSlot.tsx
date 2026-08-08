import { useImtithalakUi } from './shell';
import type { HelpRenderProps } from './shell';

/**
 * HelpSlot — the shared-lib placeholder for the host app's contextual-help "?" control. Segmented and
 * Tabs render it where they used to render on-prem's <HelpIcon/> directly; the actual control is injected
 * by the host via <ImtithalakUiProvider renderHelp={…}>. On a channel that provides no renderer (e.g. Cloud),
 * it renders nothing — so the design system stays app-agnostic and the moat's kb.json stays a host concern.
 */
export function HelpSlot({ entryId, className }: HelpRenderProps) {
  const { renderHelp } = useImtithalakUi();
  if (!renderHelp) return null;
  return <>{renderHelp({ entryId, className })}</>;
}
