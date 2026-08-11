// @imtithalak-ui — the ONE shared app shell (sidebar rail + top-less main + theme/lang/account controls),
// consumed by every channel SPA. It is PURELY PRESENTATIONAL and fully parameterized: the reference is
// the on-prem client shell, moved here byte-for-byte so both channels render an identical chrome and a
// future shell change edits THIS file only. Everything channel-specific — the nav items, brand,
// authenticated user, sign-out/account/theme/language handlers, an optional command-palette slot — is
// supplied by props. Page content renders through <Outlet/>; the collapse state persists per-browser.
import { useEffect, useState, type ReactNode } from 'react';
import { NavLink, Outlet } from 'react-router-dom';
import {
  Command,
  Languages,
  LogOut,
  Menu,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Sun,
  UserCog,
  X,
  type LucideIcon,
} from 'lucide-react';
import { Badge } from '../design-system/Badge';
import { BrandMark } from '../design-system/BrandMark';
import { Button } from '../design-system/Button';
import { Toaster } from '../design-system/Toast';
import { cn } from '../lib/cn';

/** A single sidebar entry. `label` is already translated by the channel (it doubles as the collapsed
 *  hover title), so the shell stays i18n-agnostic.
 *
 *  TWO MODES, ONE CHROME. The client is ROUTER-driven: an item has `to` and the shell renders a
 *  <NavLink>. The admin is STATE-driven (a single-page action console, no routes): an item has `key`,
 *  the shell renders a <button> and reports clicks through `onSelect`. Everything else — the rail, the
 *  brand, the user block, the toggles — is identical, which is the whole point of sharing this file.
 *  `icon` is optional so a grouped nav (whose heading carries the icon) can list plain text items. */
export interface AppShellNavItem {
  to?: string;
  key?: string;
  label: string;
  icon?: LucideIcon;
  /** Optional group id (matches an AppShellNavGroup). Ungrouped items render before any heading. */
  group?: string;
}

/** A heading that groups nav items (admin's Customers / Revenue / Catalog / System). Client passes none
 *  and stays flat. */
export interface AppShellNavGroup {
  id: string;
  label: string;
  icon?: LucideIcon;
}

/** The authenticated user shown in the sidebar footer. `roleLabel` is the pre-translated role badge; omit
 *  it to hide the badge. */
export interface AppShellUser {
  name: string;
  roleLabel?: string;
}

/** Chrome strings the channel supplies from its own i18n so the shell hard-codes no copy. */
export interface AppShellLabels {
  collapse: string;
  expand: string;
  signOut: string;
  /** Text/aria for the theme toggle (only rendered when `onToggleTheme` is given). */
  toggleTheme?: string;
  /** Text/aria for the language toggle (only rendered when `onToggleLang` is given). */
  toggleLang?: string;
  /** Aria/title for the account link (only rendered when `onAccount` is given). */
  account?: string;
}

export interface AppShellProps {
  /** Pre-filtered, role-aware nav items (the channel does its own RBAC filtering). */
  navItems: AppShellNavItem[];
  /** Optional group headings; when given, items are grouped by their `group` id in this order. */
  navGroups?: AppShellNavGroup[];
  /** STATE-DRIVEN mode (admin): the selected item key, and a click handler. When `onSelect` is set the
   *  shell renders <button>s (keyed by `key ?? to`) instead of router <NavLink>s. */
  activeKey?: string;
  onSelect?: (key: string) => void;
  /** Content rendered in the main column INSTEAD of the router <Outlet/> — for a channel without routes. */
  children?: ReactNode;
  /** `mark` is the brand glyph shown beside the name; it DEFAULTS to the platform's BrandMark, so a
   *  consuming product supplies its own (e.g. its logo) without forking the shell. */
  brand: { name: string; tagline?: string; mark?: ReactNode };
  /** The signed-in user, or null (footer collapses to controls only). */
  user: AppShellUser | null;
  labels: AppShellLabels;
  onSignOut: () => void;
  /** Theme control — the button renders only when `onToggleTheme` is provided. */
  theme?: 'dark' | 'light';
  onToggleTheme?: () => void;
  /** Language control — the button renders only when `onToggleLang` is provided (optional per channel). */
  onToggleLang?: () => void;
  /** Account link — renders only when provided (e.g. on-prem has an /account page; cloud does not). */
  onAccount?: () => void;
  /** Show the "⌘K" command-palette hint in the footer (channels with a palette). */
  showPaletteHint?: boolean;
  /** App-only extras mounted once alongside the shell (e.g. the command palette). */
  slots?: ReactNode;
  /** Optional app-wide footer rendered at the bottom of the main content column (channels with a footer). */
  footer?: ReactNode;
  /** Optional widget injected into the sidebar rail above the user block (e.g. cloud's smart wallet chip). */
  railSlot?: ReactNode;
  /** Optional global project switcher, rendered at the TOP of the sidebar above the nav (the workspace
   *  pivot — cloud's per-client Project selector). Channel-optional, so on-prem is unaffected. */
  projectSwitcher?: ReactNode;
}

// One nav entry, in either mode. Router mode (NavLink, active from the route) or state mode (a button
// that reports its key). The classes are identical so both channels render the same rail.
function renderNavItem(
  item: AppShellNavItem,
  { collapsed, activeKey, onSelect, onNavigate }: { collapsed: boolean; activeKey?: string; onSelect?: (key: string) => void; onNavigate?: () => void },
) {
  const Icon = item.icon;
  // min-h-11 (44px) is the accessible touch-target floor; py-1.5 alone rendered ~32px and failed the
  // mobile spec. The row still hugs its content width; only its HEIGHT is floored.
  const base = 'relative flex min-h-11 items-center rounded-lg py-1.5 text-sm font-medium transition-colors focus-ring';
  const layout = collapsed ? 'justify-center px-0' : 'gap-3 px-3';
  const active = 'bg-accent-soft text-accent-on-soft before:absolute before:inset-y-2 before:start-0 before:w-[3px] before:rounded-full before:bg-accent';
  const inactive = 'text-text-secondary hover:bg-surface-2 hover:text-text-primary';
  const inner = (
    <>
      {Icon && <Icon size={18} className="shrink-0" />}
      {!collapsed && item.label}
    </>
  );
  if (onSelect) {
    const k = item.key ?? item.to ?? item.label;
    return (
      <button
        key={k}
        type="button"
        onClick={() => { onSelect(k); onNavigate?.(); }}
        title={collapsed ? item.label : undefined}
        className={cn(base, layout, 'w-full text-start', activeKey === k ? active : inactive)}
      >
        {inner}
      </button>
    );
  }
  return (
    <NavLink
      key={item.to}
      to={item.to ?? '#'}
      onClick={onNavigate}
      title={collapsed ? item.label : undefined}
      className={({ isActive }) => cn(base, layout, isActive ? active : inactive)}
    >
      {inner}
    </NavLink>
  );
}

export function AppShell({
  navItems,
  navGroups,
  activeKey,
  onSelect,
  children,
  brand,
  user,
  labels,
  onSignOut,
  theme,
  onToggleTheme,
  onToggleLang,
  onAccount,
  showPaletteHint,
  slots,
  footer,
  railSlot,
  projectSwitcher,
}: AppShellProps) {
  // Collapsible sidebar: shrink to an icon-only rail for more workspace. The choice persists per browser
  // (localStorage) so it survives reloads. Collapsed → labels hidden, native hover tooltips (title=) name
  // each item; the rail keeps every control reachable as an icon.
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem('imtithalak-nav-collapsed') === '1'; } catch { return false; }
  });
  useEffect(() => {
    try { localStorage.setItem('imtithalak-nav-collapsed', collapsed ? '1' : '0'); } catch { /* ignore */ }
  }, [collapsed]);

  // #831 — on phones the fixed rail would eat the screen, so below `md` the sidebar becomes an off-canvas
  // DRAWER: a hamburger in the mobile header opens it, an overlay (or a nav tap) closes it. At `md+` it is
  // the normal static, collapsible rail — desktop is byte-for-byte unchanged.
  const [mobileOpen, setMobileOpen] = useState(false);
  const closeMobile = () => setMobileOpen(false);

  return (
    <div className="flex h-screen overflow-hidden bg-surface-0 text-text-primary">
      {/* Mobile scrim behind the drawer (below md only). */}
      {mobileOpen && (
        <button
          type="button"
          aria-label={labels.collapse}
          onClick={closeMobile}
          className="fixed inset-0 z-40 bg-overlay md:hidden"
        />
      )}
      {/* ── sidebar: role-aware grouped nav. Desktop: collapsible rail. Mobile: off-canvas drawer. ── */}
      <aside
        data-testid="sidebar"
        data-collapsed={collapsed}
        className={cn(
          'flex flex-col border-e border-border-subtle bg-surface-1',
          // mobile drawer: fixed, slides in from the start edge (works in RTL + LTR)
          'fixed inset-y-0 start-0 z-50 w-72 max-w-[82vw] -translate-x-full rtl:translate-x-full transition-transform duration-200',
          mobileOpen && '!translate-x-0 shadow-xl',
          // desktop: static in-flow rail with a collapsible width (relative = positioning ctx for the toggle)
          'md:relative md:z-auto md:w-60 md:max-w-none md:!translate-x-0 md:shrink-0 md:shadow-none md:transition-[width]',
          collapsed ? 'md:w-16' : 'md:w-60',
        )}
      >
        {/* Mobile-only: close the drawer (desktop uses the collapse handle instead). */}
        <button
          type="button"
          onClick={closeMobile}
          aria-label={labels.collapse}
          className="absolute end-3 top-3 z-10 grid size-11 place-items-center rounded-lg text-text-secondary transition hover:bg-surface-2 hover:text-text-primary md:hidden"
        >
          <X size={18} />
        </button>

        {/* Collapse toggle — pinned to the sidebar's TRAILING edge (the "back", against the divider) at a
            fixed vertical offset, so it never jumps between the expanded and collapsed layouts. It floats
            on the border, half-overhanging into the main area, the way a rail handle should. */}
        <Button
          variant="ghost"
          size="icon"
          onClick={() => setCollapsed((c) => !c)}
          aria-label={collapsed ? labels.expand : labels.collapse}
          title={collapsed ? labels.expand : labels.collapse}
          data-testid="nav-collapse"
          className="absolute top-6 z-10 hidden h-6 w-6 -translate-y-1/2 rounded-full border border-border-subtle bg-surface-1 shadow-sm end-0 translate-x-1/2 rtl:-translate-x-1/2 md:flex"
        >
          {collapsed ? (
            <PanelLeftOpen size={14} className="rtl:rotate-180" />
          ) : (
            <PanelLeftClose size={14} className="rtl:rotate-180" />
          )}
        </Button>

        {/* brand */}
        <div className={cn('flex items-center px-3 py-3.5', collapsed ? 'justify-center' : 'justify-start')}>
          {collapsed ? (
            brand.mark ?? <BrandMark size={26} className="shrink-0" />
          ) : (
            <div className="flex min-w-0 items-center gap-2.5 ps-2">
              {brand.mark ?? <BrandMark size={26} className="shrink-0" />}
              <div className="min-w-0">
                <div data-testid="brand-name" className="truncate text-base font-semibold">
                  {brand.name}
                </div>
                {brand.tagline && (
                  <div className="truncate text-xs text-text-muted">{brand.tagline}</div>
                )}
              </div>
            </div>
          )}
        </div>

        {/* optional global project switcher (the workspace pivot), pinned above the nav — expanded only
            (the collapsed icon rail has no room for it) */}
        {projectSwitcher && !collapsed && (
          <div className="px-3 pb-1" data-testid="project-switcher-slot">{projectSwitcher}</div>
        )}

        <nav className="flex-1 space-y-0.5 overflow-y-auto px-3 pt-1">
          {(navGroups && navGroups.length > 0
            ? navGroups
                .map((g) => ({ group: g, items: navItems.filter((it) => it.group === g.id) }))
                .filter((s) => s.items.length > 0)
            : [{ group: null as AppShellNavGroup | null, items: navItems }]
          ).map(({ group, items }) => (
            <div key={group?.id ?? '_flat'} className={cn('space-y-0.5', group && 'pt-2 first:pt-1')}>
              {group && !collapsed && (
                <span className="flex items-center gap-1.5 px-3 pb-1 text-[0.7rem] font-semibold uppercase tracking-[0.12em] text-text-muted">
                  {group.icon && <group.icon size={12} aria-hidden />}
                  {group.label}
                </span>
              )}
              {items.map((item) => renderNavItem(item, { collapsed, activeKey, onSelect, onNavigate: closeMobile }))}
            </div>
          ))}
        </nav>

        {/* optional channel-injected rail widget (e.g. cloud's wallet chip), shown above the user block
            only when expanded — collapsed rails stay icon-only */}
        {railSlot && !collapsed && (
          <div className="border-t border-border-subtle px-3 py-3" data-testid="rail-slot">{railSlot}</div>
        )}

        {/* logged-in user + role */}
        <div className="border-t border-border-subtle px-3 py-2.5" data-testid="current-user">
          {collapsed ? (
            <div className="flex flex-col items-center gap-1">
              {onAccount && (
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={onAccount}
                  aria-label={user?.name || labels.account}
                  title={user?.name || labels.account}
                  data-testid="account-link"
                >
                  <UserCog size={18} />
                </Button>
              )}
              <Button
                variant="ghost"
                size="icon"
                onClick={onSignOut}
                aria-label={labels.signOut}
                title={labels.signOut}
                data-testid="logout"
              >
                <LogOut size={18} />
              </Button>
            </div>
          ) : (
            <div className="rounded-md bg-surface-2 px-3 py-2">
              <div className="flex items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium" data-testid="current-username">
                    {user?.name}
                  </div>
                  {user?.roleLabel && (
                    <Badge tone="accent" className="mt-1" data-testid="current-role">
                      {user.roleLabel}
                    </Badge>
                  )}
                </div>
                {onAccount && (
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={onAccount}
                    aria-label={labels.account}
                    data-testid="account-link"
                  >
                    <UserCog size={18} />
                  </Button>
                )}
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={onSignOut}
                  aria-label={labels.signOut}
                  data-testid="logout"
                >
                  <LogOut size={18} />
                </Button>
              </div>
            </div>
          )}
        </div>

        {/* theme + language + palette hint */}
        <div
          className={cn(
            'flex items-center gap-2 border-t border-border-subtle px-3 py-2',
            collapsed && 'flex-col',
          )}
        >
          {onToggleTheme && (
            <Button
              variant="ghost"
              size="icon"
              onClick={onToggleTheme}
              aria-label={labels.toggleTheme}
              title={labels.toggleTheme}
              data-testid="theme-toggle"
            >
              {theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}
            </Button>
          )}
          {onToggleLang && (
            <Button
              variant="ghost"
              size={collapsed ? 'icon' : 'sm'}
              onClick={onToggleLang}
              className={collapsed ? undefined : 'gap-2'}
              aria-label={labels.toggleLang}
              title={labels.toggleLang}
              data-testid="lang-toggle"
            >
              <Languages size={16} />
              {!collapsed && labels.toggleLang}
            </Button>
          )}
          {showPaletteHint && !collapsed && (
            <span className="ms-auto flex items-center gap-1 text-[10px] text-text-muted">
              <Command size={12} />K
            </span>
          )}
        </div>
      </aside>

      {/* flex column so the footer (when present) is pinned to the BOTTOM: the content wrapper grows to fill
          the viewport, so on a short page the footer sits at the bottom edge — never floating mid-screen —
          and on a long page it flows after the content. */}
      <main className="flex min-w-0 flex-1 flex-col overflow-x-hidden overflow-y-auto">
        {/* Mobile header (below md only): a hamburger opens the drawer, with the brand beside it. On desktop
            there is no global header bar — each page owns its own filters next to its content. */}
        <div className="sticky top-0 z-30 flex items-center gap-2.5 border-b border-border-subtle bg-surface-0/90 px-4 py-2.5 backdrop-blur-md md:hidden">
          <button
            type="button"
            onClick={() => setMobileOpen(true)}
            aria-label={labels.expand}
            className="grid size-9 place-items-center rounded-lg text-text-secondary transition hover:bg-surface-2 hover:text-text-primary"
          >
            <Menu size={20} />
          </button>
          <div className="flex items-center gap-2">
            {brand.mark ?? <BrandMark size={22} className="shrink-0" />}
            <span className="text-sm font-semibold">{brand.name}</span>
          </div>
        </div>
        <div className="mx-auto w-full max-w-5xl flex-1 p-4 sm:p-6 md:p-8">
          {children ?? <Outlet />}
        </div>
        {footer}
      </main>

      {/* app-only extras (e.g. the command palette + shortcuts), mounted once */}
      {slots}
      {/* transient feedback host (copy confirmations, etc.) */}
      <Toaster />
    </div>
  );
}
