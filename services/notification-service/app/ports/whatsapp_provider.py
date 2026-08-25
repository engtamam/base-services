"""WhatsAppProvider port — send one WhatsApp message.

WhatsApp is template-shaped, not free text: a business-initiated message must reference a template
Meta has APPROVED, with its variables substituted. So `send` carries the approved template NAME, its
language, and the ordered variables — never a rendered body the way email/SMS do.

Returns the PROVIDER MESSAGE ID (WhatsApp's `wamid`) on success, or "" on failure. The wamid is the
only join key between a send and the delivery/read receipt that arrives later by webhook — Cloud API
returns it as messages[0].id and echoes the identical value as statuses[].id — so a provider that
discards it makes every receipt unattributable. "" is falsy, so `if provider.send(...)` still reads as
success/failure exactly as a bool did.

`biz_opaque_callback_data` is an optional arbitrary string (≤512 chars) the caller may round-trip:
Cloud API echoes it verbatim in the status webhook, letting a caller correlate against its own ledger
with no join and no extra read. Providers that cannot forward it ignore it.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class WhatsAppProvider(Protocol):
    driver: str

    def send(self, to: str, template_name: str, language: str, variables: list[str],
             biz_opaque_callback_data: str = "") -> str: ...

    def send_interactive(self, to: str, interactive: dict,
                         biz_opaque_callback_data: str = "") -> str:
        """Send an interactive (reply-buttons / list) message, INSIDE the 24h customer-service window
        a customer's own inbound message opens — so no template is required. `interactive` is the
        fully-built Meta `interactive` object (see core/whatsapp_interactive.build_button/build_list);
        the provider only transmits it. Returns the wamid on success, "" on failure, exactly like
        send(). The wamid remains the join key to the reply/receipt that arrives later by webhook.
        """
        ...
