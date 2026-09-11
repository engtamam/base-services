"""The settings an operator may change at runtime, for this service.

DECLARING A KEY HERE IS A PROMISE THAT SOMETHING READS IT through RuntimeConfig at the point of
use. Offering every settable field to the panel instead fails in the most convincing way available:
the panel accepts the change, the audit log records it with an actor and a timestamp, and behaviour
does not move, because nothing consults the override. The operator ends up holding a receipt for a
change that never happened.

Add a key here in the same change that wires the read, never before. Anything absent is refused,
which is the safe direction — unavailable is honest, inert is not.
"""
from __future__ import annotations

# PAYMENT_GATEWAY_DRIVER — the active payment gateway, per tenant, changeable after launch. READ in
# app/api/v1/billing.py's build_billing (via RuntimeConfig) and applied by container.rebuild_gateway,
# which is the promise this file makes. A switch to a driver that is not configured fails CLOSED (a
# gateway whose charge raises GatewayUnavailable) rather than charging through the boot provider.
# BILLING_CLAWBACK_ENABLED — whether a refund/chargeback reverses the wallet grant. READ per tenant
# in build_billing and applied in _claw_back. The other clawback knobs (allow-negative, on-partial,
# skip-expired) are deploy-time config, not per-tenant runtime — only the master switch is here.
# BILLING_DOCUMENT_MODE — which money document a paid invoice produces (receipt | tax_invoice |
# both). READ per tenant in build_billing and applied in _mark_paid (the ZATCA e-invoice is finalized
# only for tax_invoice/both) and the document routes. Default `receipt`: a fresh platform issues our
# own receipt with no VAT/ZATCA until it opts into tax.
#
# PLAN_CREDIT_PERCENT — the included allowance as a percent of a plan's price, for plans that opt in
# with credit_percent_of_price=True. READ (global scope) in build_billing and applied at grant time in
# _compute_renewal's _granted_allowance. This is the promise: the panel can change "how much is
# included" live, and the next cycle grants the new percentage — no redeploy, no plan reseed.
#
# THE TAX PROFILE IS ONE UNIT. The mode was live-editable while the profile behind it — the tax
# calculator, the VAT rate, the seller's registration number — was read from static settings at boot.
# That split WAS the whole defect: one click flipped a tenant to tax_invoice and the next paid invoice
# rendered a document titled "tax invoice" carrying 0% VAT and no seller number — formally well-shaped,
# legally void, and silent. So the rest of the profile is brought in here AS ONE UNIT: each key below
# is READ in build_billing at the point of use, and a mode that issues a tax invoice is refused —
# at the operator write AND at render time — unless the calculator, rate and seller number behind it
# are all present (see app/core/tax_profile.py). Editing any of them from the panel is now atomic with
# the mode, and completeness is enforced across all of them together.
RUNTIME_POLICY: frozenset[str] = frozenset({
    "PAYMENT_GATEWAY_DRIVER", "BILLING_AUTO_REACTIVATE", "BILLING_CLAWBACK_ENABLED",
    "BILLING_DOCUMENT_MODE",
    "TAX_CALCULATOR_DRIVER", "VAT_RATE", "EINVOICER_DRIVER",
    "INVOICE_SELLER_NAME", "INVOICE_SELLER_VAT", "INVOICE_SELLER_CR", "INVOICE_SELLER_ADDRESS",
    "PLAN_CREDIT_PERCENT"})
