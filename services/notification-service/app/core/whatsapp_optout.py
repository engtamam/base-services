"""Apply a WhatsApp `user_preferences` webhook (marketing opt-out) to durable preferences.

WHY THIS IS NOT A BUS SUBSCRIBER. The event bus is at-most-once, best-effort (see ports/event_bus):
a pod restart at the wrong second drops the event forever. Capturing a marketing OPT-OUT that way
would rebuild the exact compliance hole this closes — we would resume sending marketing to someone
who unsubscribed, and Meta suspends business accounts for precisely that. So the opt-out is written
DURABLY here, synchronously in the webhook request, the same way inbound messages and receipts are
persisted before the (hint-only) bus publish. The bus event still fires for anything that wants a
live nudge; it is never the record of record.

Meta's shape, verified against the current first-party docs (2026-09-10):
  changes[].field == "user_preferences"
  changes[].value.user_preferences[] = [{wa_id, detail, category:"marketing_messages",
                                          value:"stop"|"resume", timestamp}]
  changes[].value.metadata.phone_number_id
`stop` opts the customer out of that category; `resume` opts them back in. Keyed by the customer's
wa_id (their phone), which for a WhatsApp send IS the recipient the send path checks.
"""
from __future__ import annotations

from typing import Any

# Meta's opt-out category -> our internal event/purpose name (what a send is tagged with and what
# Preference.opt_out_events / the consent map key on). Only marketing is opt-out-able today; a new
# category Meta adds is IGNORED until it is mapped here, never guessed onto a wrong event.
_CATEGORY_TO_EVENT = {"marketing_messages": "marketing"}

_STOP, _RESUME = "stop", "resume"


def wa_pref_key(tenant: str, phone: str) -> str:
    """The preference key for a WhatsApp marketing opt-out — SCOPED by (tenant, phone). Keying on the
    phone ALONE made an opt-out global: unsubscribing from one shop silenced every shop's marketing to
    that number, and a `resume` for one re-enabled all of them (notification-r2-02). Tenant is
    length-prefixed so no (tenant, phone) pair can be forged to collide. An empty tenant (the number is
    not bound to any shop) yields a GLOBAL key, which the send path also consults — the compliance-safe
    fallback (suppress everywhere) when we cannot attribute the number."""
    return f"{len(tenant)}:{tenant}|wa:{phone}"


async def apply_user_preferences(prefs, value: dict[str, Any], *, number_index=None) -> int:
    """Apply one webhook change's `value` to the PreferenceStore. Returns how many entries applied.

    Scoped per (tenant, phone): the tenant is resolved from value.metadata.phone_number_id via the
    WhatsApp number index, so one shop's opt-out no longer suppresses another's (notification-r2-02).

    Defensive: an unknown category or an unexpected `value` is skipped, not misapplied — mislabelling
    a resume as a stop (or vice versa) is itself a compliance error.
    """
    phone_number_id = str((value.get("metadata") or {}).get("phone_number_id", "") or "")
    tenant = ""
    if number_index is not None and phone_number_id:
        tenant = (await number_index.tenant_of(phone_number_id)) or ""
    applied = 0
    for entry in (value.get("user_preferences") or []):
        wa_id = str(entry.get("wa_id", "") or "")
        event = _CATEGORY_TO_EVENT.get(str(entry.get("category", "") or ""), "")
        decision = str(entry.get("value", "") or "").lower()
        if not wa_id or not event or decision not in (_STOP, _RESUME):
            continue
        await prefs.apply_event_opt_out(wa_pref_key(tenant, wa_id), event,
                                        opted_out=(decision == _STOP))
        applied += 1
    return applied
