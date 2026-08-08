import { NavLink } from 'react-router-dom';
import type { LucideIcon } from 'lucide-react';
import { cn } from '../lib/cn';
import { HelpSlot } from './HelpSlot';

/**
 * TabBar — the in-workspace switcher (no route-bounce feel, no scroll between unrelated views). Each tab
 * is a nested-route NavLink so deep-linking + the back button work, and only ONE tab's panel renders via
 * the workspace <Outlet/>. Theme-token styled, RTL-aware, keyboard reachable (NavLink = real anchors).
 */
export interface TabDef {
  to: string;
  label: string;
  icon?: LucideIcon;
  testId?: string;
  /** Optional knowledge-base key — renders a contextual-help (?) icon after the label. */
  help?: string;
}

export function TabBar({ tabs, className }: { tabs: TabDef[]; className?: string }) {
  if (tabs.length <= 1) return null; // a single-tab workspace shows no bar
  return (
    <div
      role="tablist"
      className={cn(
        // overflow-x-auto lets a crowded tab row scroll sideways; overflow-y-hidden is REQUIRED — without
        // it the spec computes overflow-y to `auto`, and the 1px the active underline/-mb-px spill below the
        // box then summon a vertical scrollbar (its ▲▼ spin buttons were showing in every tab header).
        'flex items-center gap-1 overflow-x-auto overflow-y-hidden border-b border-border-subtle',
        className,
      )}
    >
      {tabs.map(({ to, label, icon: Icon, testId, help }) => (
        <NavLink
          key={to}
          to={to}
          role="tab"
          data-testid={testId}
          className={({ isActive }) =>
            cn(
              'group relative -mb-px flex items-center gap-2 whitespace-nowrap rounded-t-md px-3.5 py-2.5 text-sm font-medium transition-colors focus-ring',
              isActive
                ? 'text-accent-on-soft'
                : 'text-text-secondary hover:bg-surface-2 hover:text-text-primary',
            )
          }
        >
          {({ isActive }) => (
            <>
              {Icon && <Icon size={16} />}
              {label}
              {help && <HelpSlot entryId={help} />}
              {/* the active underline, in the accent — drawn on top of the bar's border */}
              <span
                className={cn(
                  // bottom-0 (not -bottom-px): keep the underline fully INSIDE the box so overflow-y-hidden
                  // on the tablist can't clip it — it still reads as the active indicator on the divider.
                  'absolute inset-x-2 bottom-0 h-0.5 rounded-full bg-accent transition-opacity',
                  isActive ? 'opacity-100' : 'opacity-0',
                )}
              />
            </>
          )}
        </NavLink>
      ))}
    </div>
  );
}
