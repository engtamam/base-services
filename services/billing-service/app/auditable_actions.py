"""billing-service — auditable actions.

Money moving OUT is the critical direction. A refund or a reversal takes real value out of the
platform, and an unattributable one is indistinguishable from fraud. Money coming in,
subscriptions and plan changes are recorded but never block: the invoice and the ledger already
hold them, and refusing a customer's upgrade because the audit log is down protects nobody.

See services/_template/app/auditable_actions.py for the format, the four sources, and how to decide
`critical` (it REFUSES the request when the audit log is unavailable, so it is a behaviour and not
a severity label).
"""
from __future__ import annotations

from typing import Any

AUDITABLE_ACTIONS: list[dict[str, Any]] = [
    {
        "action": "plan.created",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "coupon.created",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "subscription.created",
        "source": "user",
        "critical": False,
    },
    {
        "action": "subscription.changed",
        "source": "user",
        "critical": False,
    },
    {
        "action": "subscription.payment_method_updated",
        "source": "user",
        "critical": False,
    },
    {
        "action": "subscription.scheduled_change_cancelled",
        "source": "user",
        "critical": False,
    },
    {
        "action": "subscription.cancelled",
        "source": "user",
        "critical": False,
    },
    {
        "action": "subscription.migrated",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "usage.recorded",
        "source": "internal",
        "critical": False,
    },
    {
        "action": "invoice.settled",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "dunning.run",
        "source": "system",
        "critical": False,
    },
    {
        "action": "payment.refunded",
        "source": "admin",
        "critical": True,
        "why": "a refund moves real money out; an unattributable refund is indistinguishable "
               "from fraud, and it is the classic insider path",
    },
    {
        "action": "settlement.reversed",
        "source": "admin",
        "critical": True,
        "why": "reversing a settlement unwinds money that was already paid out; without a "
               "record there is no way to say who authorised it",
    },
    {
        "action": "subject.erased",
        "source": "internal",
        "critical": True,
        "why": "erasure is irreversible and the data is gone — nothing but this record can "
               "show we honoured the DSAR, or prove we erased the right person",
    },
    {
        "action": "operator.secret_set",
        "source": "admin",
        "critical": True,
        "resource": "secret",
        "why": "a credential change is the action that most needs attribution: the previous value "
               "is gone the moment it happens, so an unrecorded rotation cannot be reconstructed, "
               "and an unattributable one is indistinguishable from an attacker's",
    },
    {
        "action": "operator.secret_cleared",
        "source": "admin",
        "critical": True,
        "resource": "secret",
        "why": "clearing a credential silently reverts the service to whatever the environment "
               "holds, which is a behaviour change nobody would otherwise see",
    },
    {
        "action": "operator.config_changed",
        "source": "admin",
        "critical": False,
        "resource": "config",
        "why": "policy changes explain later behaviour — why a sweep deleted more, why a limit "
               "started refusing — and that history is unavailable anywhere else. Not critical: "
               "refusing to change a retention period because the log is down protects nothing",
    },
    {
        "action": "plan.updated",
        "source": "admin",
        "critical": True,
        "resource": "plan",
        "why": "a plan is the terms money is taken on. An unattributable price change is "
               "indistinguishable from an attacker's, and the previous price is overwritten by "
               "the change itself — so without this nobody can reconstruct what was agreed",
    },
    {
        "action": "plan.availability_changed",
        "source": "admin",
        "critical": True,
        "resource": "plan",
        "why": "retiring a plan stops all new revenue on it; doing that by accident is a silent "
               "outage of the signup funnel that nothing else would surface",
    },
    {
        "action": "coupon.updated",
        "source": "admin",
        "critical": True,
        "resource": "coupon",
        "why": "raising a discount or extending a campaign changes what the platform earns, and "
               "the previous terms are overwritten by the edit — without this nobody can say what "
               "the coupon was worth yesterday",
    },
    {
        "action": "coupon.deleted",
        "source": "admin",
        "critical": True,
        "resource": "coupon",
        "why": "the coupon and its redemption history are gone; if it is not recorded here there "
               "is no evidence the campaign ever existed",
    },
    {
        "action": "billing.platform_invoices_viewed",
        "source": "admin",
        "critical": False,
        "resource": "invoices",
        "why": "a cross-tenant read is a privileged act: it sees every customer's money, so who "
               "looked across all tenants and when is worth recording even though it changes "
               "nothing",
    },
    {
        "action": "payment.reconciled",
        "source": "admin",
        "critical": True,
        "resource": "payment",
        "why": "reconciliation pulls a gateway's status to recover a missed webhook and can move a "
               "payment to paid — the same money-moving effect as the webhook it stands in for, so "
               "who triggered it and against which payment must be recorded",
    }
]
