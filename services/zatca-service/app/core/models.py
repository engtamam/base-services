"""Domain models — the generic invoice input + the persisted ZATCA record + settings."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


def new_id() -> str:
    return uuid.uuid4().hex


# Status lifecycle: none → pending → signed → submitted → cleared/reported/rejected
STATUSES = ("none", "pending", "signed", "submitted", "cleared", "reported", "rejected")
ACCEPTED = ("cleared", "reported")


@dataclass
class ZatcaInvoice:
    id: str
    tenant: str
    number: str
    uuid: str = ""
    icv: int = 0
    pih: str = ""
    hash: str = ""                       # this invoice's hash = next invoice's PIH
    status: str = "none"
    invoice_type: str = "simplified"     # simplified | standard
    signed_xml: str = ""
    qr: str = ""                         # base64 TLV
    cleared_xml: str = ""                # ZATCA-stamped legal XML (clearance success)
    attempts: int = 0
    next_retry_at: float | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    message: str = ""
    created: str = ""
    draft: dict[str, Any] = field(default_factory=dict)   # unsigned UBL + sign params (for re-sign)

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in (
            "id", "tenant", "number", "uuid", "icv", "pih", "hash", "status", "invoice_type",
            "signed_xml", "qr", "cleared_xml", "attempts", "next_retry_at",
            "warnings", "errors", "message", "created", "draft")}

    def public(self) -> dict[str, Any]:
        """Admin view — omits the heavy XML bodies + the draft."""
        d = self.to_dict()
        for k in ("signed_xml", "cleared_xml", "draft"):
            d.pop(k, None)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ZatcaInvoice":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})
