# Rotating a credential

Every service that holds provider credentials exposes the same six routes under
`/api/v1/operator`. This is what an operator needs to know to use them without causing an outage.

## The two windows, and why they must overlap

| | what it is | setting |
|---|---|---|
| **rotation window** | how long the **previous** credential stays valid after a new one is set | `SECRETS_ROTATION_WINDOW_SECONDS` (default 300) |
| **refresh window** | how long a replica keeps using a credential it already resolved | `PROVIDER_REFRESH_SECONDS` and friends (default 30) |

The refresh window must never exceed the rotation window. If it does, there is a period where a
live replica is still sending the old credential but the old credential has already expired —
every call failing, after a change that reported success in the panel. A boot guard refuses that
configuration rather than trusting anyone to remember it.

A rotation is therefore visible within the refresh window, not instantly. The replica that served
the write drops its own cache immediately; the others catch up. This is deliberate: resolving a
credential per request would put a datastore read and a scrypt-backed decrypt on every send,
charge, or clearance.

## Setting or rotating one

```
PUT /api/v1/operator/secrets
{"name": "TAQNYAT_TOKEN", "value": "<the new credential>", "tenant": ""}
```

- `name` is the **setting it replaces**, so there is one vocabulary rather than two.
- `tenant` empty means platform-wide. A tenant value applies to that tenant only and takes
  precedence over the platform one.
- Requires `platform.secrets`. That capability is deliberately separate from `platform.config`:
  setting a payment key and changing a retention period are not the same privilege.
- The response contains a four-character **hint**, never the value.

**Nothing reads a credential back.** `GET /operator/secrets` lists names, hints and whether a
rotation is in progress. There is no route that returns a value and `safety:no-secret-reads` fails
the build if one is ever added. If you lose a credential, reissue it at the provider — this system
cannot recover it for you, by design.

## Before you rotate

1. `GET /api/v1/operator/secrets` and check the hint of the one you are replacing. The hint exists
   for exactly this: replacing the wrong credential is the common mistake.
2. Set the new value. The response shows `"rotating": true`, meaning the previous value is still
   being accepted.
3. Exercise the path — send a test notification, run a test charge — **inside** the rotation
   window, while both values still work. If the new credential is wrong, set the old one back; it
   has not been discarded yet.

## Inbound secrets are different

Where a service verifies **someone else's** signature — payment webhooks, inbound webhook sources —
both the current and previous secrets are accepted for the rotation window, because a callback
already in flight was signed with whatever the sender held when it sent. There is no version to
negotiate. Rejecting those looks exactly like a forgery, and for a payment callback it means an
invoice stays unpaid after the customer's money has already moved.

Rotate the secret **at the provider first**, then here, or allow the window to cover the gap.

## What is not settable, and why

| stays deploy-time | reason |
|---|---|
| `SECRETS_KEK` | it seals everything else. A key editable in the panel protects nothing from someone who has the panel. |
| service seal keys (`ZATCA_SEAL_KEY`, `SEAL_KEY`, `CREDENTIAL_SEAL_KEY`) | same class. Rotating one makes every stored certificate or credential unreadable **at once** rather than one at a time. |
| token signing material (`TOKEN_HS256_SECRET`, `TOKEN_PRIVATE_KEY`) | changing it under live sessions invalidates every issued token: everyone logged out, every service seeing forgeries. That is a planned migration with key overlap, not a form field. |
| `PII_SALT` | frozen. Change it and every pseudonym re-maps — history orphans, and an erasure can no longer prove it erased the right person. |
| driver/DSN wiring | an adapter swapped under a live request is a class of bug nobody can reproduce. |

Attempting any of these returns `400` with one message for both "not settable" and "no such
setting" — distinguishing them would turn the panel into an oracle for the internal settings list.

## Runtime settings

`GET /api/v1/operator/config` shows only the settings this service **actually resolves at runtime**,
declared in its `app/runtime_policy.py`. A knob absent from that list is refused.

That is the safe direction. Offering every settable field instead fails in the most convincing way
available: the panel accepts the change, the audit log records it with an actor and a timestamp,
and behaviour does not move, because nothing consults the override — leaving an operator holding a
receipt for a limit that was never raised.

## Without a KEK

If `SECRETS_KEK` is unset, the operator surface returns `503` rather than storing anything. There is
nothing to seal with, and writing the credential anyway — in the clear, while every check reports
"encrypted" — is the one outcome that must never happen. Services keep running on their configured
credentials.

## What is recorded

`operator.secret_set` and `operator.secret_cleared` are **critical** audit actions, written
**before** the change lands: the previous value is gone the moment it happens, so an unrecorded
rotation cannot be reconstructed, and an unattributable one is indistinguishable from an attacker's.
If the audit log cannot be reached, the rotation is refused.

The actor is a pseudonym, never a name — the entry lands in an append-only chain, and an
append-only record of an identity is one nobody can erase later.
