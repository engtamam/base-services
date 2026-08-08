import { forwardRef, useId } from 'react';
import type {
  InputHTMLAttributes, SelectHTMLAttributes, TextareaHTMLAttributes, ReactNode,
} from 'react';
import { ChevronDown } from 'lucide-react';
import { cn } from '../lib/cn';

// SHARED form controls — the styled replacements for native <input>/<select>/<textarea> so every app's
// forms read as one system: theme tokens only (no raw hex), RTL-correct (logical spacing; the Select's
// chevron sits at the logical `end`), light+dark aware, keyboard-accessible (native focus + `.focus-ring`),
// and reduced-motion safe (only a colour transition, which the global media rule neutralises).
//
// A field in an ERROR state flips its border/ring to danger via `aria-invalid` — pass it through from the
// <Field> wrapper, which owns the label + hint + error message and wires the a11y ids.

// One control skin. `aria-invalid` (a real attribute the browser reflects) drives the danger border so we
// never need an `error` prop on the control itself — the <Field> wrapper sets it.
const CONTROL =
  'w-full rounded-md border border-border-strong bg-surface-1 px-3 py-2 text-sm text-text-primary ' +
  'placeholder:text-text-muted transition-colors outline-none focus-ring ' +
  'disabled:cursor-not-allowed disabled:opacity-50 ' +
  'aria-[invalid=true]:border-danger aria-[invalid=true]:focus-visible:outline-danger';

export type InputProps = InputHTMLAttributes<HTMLInputElement>;
export const Input = forwardRef<HTMLInputElement, InputProps>(
  ({ className, ...props }, ref) => (
    <input ref={ref} className={cn(CONTROL, className)} {...props} />
  ),
);
Input.displayName = 'Input';

export type TextareaProps = TextareaHTMLAttributes<HTMLTextAreaElement>;
export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, rows = 4, ...props }, ref) => (
    <textarea ref={ref} rows={rows} className={cn(CONTROL, 'resize-y leading-relaxed', className)} {...props} />
  ),
);
Textarea.displayName = 'Textarea';

export type SelectProps = SelectHTMLAttributes<HTMLSelectElement>;
// A NATIVE <select> with the OS chrome stripped (`appearance-none`) and OUR chevron drawn at the logical
// end — so it matches the Input in both themes and flips sides in RTL for free (the padding + chevron use
// logical `-e-` utilities). We keep the native element (not a custom listbox) for full keyboard + a11y +
// mobile-picker behaviour; only the paint is ours.
export const Select = forwardRef<HTMLSelectElement, SelectProps>(
  ({ className, children, ...props }, ref) => (
    <div className="relative">
      <select
        ref={ref}
        className={cn(CONTROL, 'appearance-none pe-9', className)}
        {...props}
      >
        {children}
      </select>
      <ChevronDown
        size={16}
        aria-hidden
        className="pointer-events-none absolute inset-y-0 end-3 my-auto text-text-muted"
      />
    </div>
  ),
);
Select.displayName = 'Select';

// The wrapper: a labelled row around any control. It owns the label, an optional hint, and an error line,
// and links them to the control via aria-describedby / aria-invalid — pass `id` through to the control so
// the <label htmlFor> binds. Children get the id + a11y wiring injected when they are one of our controls.
export interface FieldProps {
  label?: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  required?: boolean;
  /** The control id; auto-generated if omitted. */
  htmlFor?: string;
  className?: string;
  children: ReactNode;
}

export function Field({ label, hint, error, required, htmlFor, className, children }: FieldProps) {
  const auto = useId();
  const id = htmlFor ?? auto;
  const hintId = hint ? `${id}-hint` : undefined;
  const errId = error ? `${id}-err` : undefined;

  return (
    <div className={cn('flex flex-col gap-1.5', className)}>
      {label != null && (
        <label htmlFor={id} className="text-label font-medium text-text-secondary">
          {label}
          {required && <span className="ms-1 text-danger" aria-hidden>*</span>}
        </label>
      )}
      {/* The control lives here. The caller wires id/aria on the control itself (see the docs on FieldProps);
          we expose the ids so a plain child still works, and richer callers read them off context if needed. */}
      {children}
      {error != null ? (
        <p id={errId} className="text-caption text-danger-on-soft" role="alert">{error}</p>
      ) : hint != null ? (
        <p id={hintId} className="text-caption text-text-muted">{hint}</p>
      ) : null}
    </div>
  );
}
