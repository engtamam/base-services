import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { Modal } from './Modal';
import { Button } from './Button';
import { Input } from './Field';

// SHARED ConfirmDialog — the in-app replacement for window.confirm() across all three apps. Built on the
// shared <Modal> (scrim, escape/click-out, body-scroll lock, RTL-safe logical spacing) so a confirm is
// two clicks in place, never a browser chrome popup that can't be styled, translated or made RTL.
//
// TWO WEIGHTS OF ASKING (mirrors the admin ActionForm):
//   - default: a plain question + Cancel / Confirm.
//   - ARMED (`confirmText` set): a destructive act must be typed back — the primary button stays disabled
//     until the operator types the exact `confirmText` (e.g. the record's name). Typing a constant trains
//     the reflex; typing the target makes you read it. Matching is trimmed but case-sensitive.
//
// Copy is passed in (localised by the caller); English defaults keep it usable without wiring i18n.
export interface ConfirmDialogProps {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: ReactNode;
  /** Supporting line under the title. */
  body?: ReactNode;
  /** Extra content between the body and the actions (a warning list, a preview). */
  children?: ReactNode;
  confirmLabel?: ReactNode;
  cancelLabel?: ReactNode;
  /** Tints the primary button as destructive (danger) rather than solid accent. Defaults to true. */
  danger?: boolean;
  /** Disables the actions while the confirmed operation is in flight. */
  busy?: boolean;
  /** ARM MODE — when set, the operator must type this exact string to enable Confirm. */
  confirmText?: string;
  /** Label above the arm input (e.g. "Type the workspace name to confirm"). */
  armLabel?: ReactNode;
}

export function ConfirmDialog({
  open, onClose, onConfirm, title, body, children,
  confirmLabel = 'Confirm', cancelLabel = 'Cancel', danger = true, busy = false,
  confirmText, armLabel,
}: ConfirmDialogProps) {
  const [typed, setTyped] = useState('');

  // Reset the arm input whenever the dialog opens/closes or the target changes, so a previous answer never
  // leaks into the next confirm.
  useEffect(() => { if (!open) setTyped(''); }, [open]);
  useEffect(() => { setTyped(''); }, [confirmText]);

  const armed = confirmText != null && confirmText !== '';
  const typedOk = !armed || typed.trim() === confirmText.trim();
  const ready = typedOk && !busy;

  return (
    <Modal open={open} onClose={onClose} title={title} subtitle={body}>
      <div className="flex flex-col gap-4">
        {children}

        {armed && (
          <label className="flex flex-col gap-1.5">
            <span className="text-label font-medium text-text-secondary">
              {armLabel ?? (
                <>Type <span className="font-semibold text-text-primary">{confirmText}</span> to confirm</>
              )}
            </span>
            <Input
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              autoComplete="off"
              autoCapitalize="off"
              spellCheck={false}
            />
          </label>
        )}

        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={busy}>{cancelLabel}</Button>
          <Button variant={danger ? 'danger' : 'solid'} onClick={onConfirm} disabled={!ready}>
            {confirmLabel}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
