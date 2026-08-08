import { cn } from '../lib/cn';
import { HelpSlot } from './HelpSlot';

export interface SegmentedOption<T extends string> {
  value: T;
  label: string;
  hint?: string;
}

interface SegmentedProps<T extends string> {
  options: SegmentedOption<T>[];
  value: T | null;
  onChange: (value: T) => void;
  className?: string;
  ariaLabel?: string;
  testId?: string;
  /** Render the control read-only (e.g. an env switcher pinned to Prod on the dashboard). */
  disabled?: boolean;
  /** kb.json key — renders a contextual-help "?" after the buttons explaining the choices. */
  help?: string;
}

// Segmented control (adapted from the Owner Hub ui-kit) — preferred over a dropdown for small, fixed
// choice sets. RTL-safe (flex + logical spacing); keyboard-reachable (real buttons, focus-ring).
export function Segmented<T extends string>({
  options,
  value,
  onChange,
  className,
  ariaLabel,
  testId,
  disabled,
  help,
}: SegmentedProps<T>) {
  const group = (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      aria-disabled={disabled || undefined}
      data-testid={testId}
      className={cn(
        'inline-flex flex-wrap gap-1 rounded-lg border border-border-subtle bg-surface-2 p-1',
        disabled && 'opacity-60',
        className,
      )}
    >
      {options.map((opt) => {
        const active = opt.value === value;
        return (
          <button
            key={opt.value}
            type="button"
            role="radio"
            aria-checked={active}
            disabled={disabled}
            title={opt.hint}
            data-testid={testId ? `${testId}-${opt.value}` : undefined}
            onClick={() => onChange(opt.value)}
            className={cn(
              'rounded-md px-3 py-1.5 text-sm font-medium transition-colors focus-ring',
              disabled && 'cursor-not-allowed',
              active
                ? 'bg-accent text-accent-contrast shadow-sm'
                : 'text-text-secondary hover:bg-surface-3 hover:text-text-primary',
            )}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );

  if (!help) return group;
  return (
    <span className="inline-flex items-center">
      {group}
      <HelpSlot entryId={help} />
    </span>
  );
}
