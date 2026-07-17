"""notification-service — end-user (self-service) actions: a person's OWN inbox.

The require_user self routes: read my notification feed, mark one read, view and update my delivery
preferences (opt-outs). The feed and preferences are keyed to the caller (from the token), so a user
only ever sees their own. `notifications.self` is the 'my own inbox' capability, advertised in the
manifest. Authoring TEMPLATES (notifications.templates) is an operator surface — it lives in
admin_actions, not here. Every action re-checks auth and scopes to the caller; this file only decides
what the app shows.
"""
from __future__ import annotations

from typing import Any


USER_ACTIONS: list[dict[str, Any]] = [
    {"id": "notifications.feed", "label": "My notifications", "method": "GET",
     "path": "/api/v1/notifications", "capability": "notifications.self", "group": "Inbox"},
    {"id": "notifications.mark_read", "label": "Mark as read", "method": "POST",
     "path": "/api/v1/notifications/{id}/read", "capability": "notifications.self", "group": "Inbox",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "notifications.get_preferences", "label": "View my preferences", "method": "GET",
     "path": "/api/v1/notifications/preferences", "capability": "notifications.self",
     "group": "Preferences"},
    {"id": "notifications.update_preferences", "label": "Update my preferences", "method": "PUT",
     "path": "/api/v1/notifications/preferences", "capability": "notifications.self",
     "group": "Preferences", "confirm": True},
]
