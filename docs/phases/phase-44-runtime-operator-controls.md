# Phase 44 — runtime operator controls

## The idea

**An operator must be able to run the platform after launch without a redeploy.**

Today every provider credential and every policy value is an environment variable chosen at boot.
That is safe and reproducible, and it means the person who owns the business cannot do their job:
rotating an SMS key, entering ZATCA credentials, changing a retention period or switching payment
provider all require an engineer and a deployment. On a live SaaS that is not a security posture,
it is a bottleneck that gets worked around — usually by handing someone shell access, which is
worse than the thing it was protecting.

So this phase moves the line. It does not remove it.

## The line

**One thing stays at deploy time: the master key.**

Provider credentials are entered in the admin panel, encrypted with a key-encryption-key that lives
in the secret store, and written to the datastore. You cannot bootstrap secrecy from a form — if
the key that protects the secrets were itself settable in the UI, an attacker with panel access
would own everything. So the root of trust stays outside, and everything above it becomes
manageable.

| stays deploy-time | becomes runtime |
| --- | --- |
| the key-encryption-key (`SECRETS_KEK`) | provider API keys and credentials |
| which datastore / cache / queue driver | which provider is preferred, and failover order |
| frozen identifiers (`PII_SALT`) | limits, quotas, retention days, grace periods |
| anything that changes data shape | thresholds, templates, branding, pattern lists |

**The test for which side a setting belongs on:** if a business person could reasonably decide it,
it is runtime. If getting it wrong breaks startup, loses data, or leaks a secret, it stays in
config where it fails fast and is reproducible.

## Decisions

**Envelope encryption, and the KEK never leaves the environment.** Credentials are sealed with
AES-256-GCM under a key derived from `SECRETS_KEK`. A stolen database yields ciphertext. A stolen
database *and* the environment yields the secrets — which is the same exposure as today, so this
adds a layer rather than removing one.

**Write-only, always.** A credential can be set and replaced; it can never be read back. The panel
shows a masked hint — last four characters — so an operator can tell which key is loaded without
the API ever returning one. There is no endpoint that returns a stored secret, and that is
enforced by a gate rather than by discipline.

**Verified before it is stored.** A credential is tested against its provider first, and a failing
one is refused. The alternative is discovering the key was wrong at 3am, with the working one
already overwritten — the moment a rotation feature turns into an outage.

**Rotation keeps the previous credential briefly.** Requests in flight during a rotation must not
fail. The old value is retained for a bounded window and then dropped, which is also what makes an
accidental rotation recoverable.

**A credential has its own capability.** `platform.secrets` is not `platform.admin`. Setting a
payment key and reading a report are different privileges, and a panel that conflates them hands
every report-reader the ability to redirect money.

**Every change is audited as critical, and the value never appears.** Who set which credential,
when, from where — and the ciphertext, the plaintext and the masked hint all stay out of the log.
An unprovable credential change is indistinguishable from an attacker's.

**Runtime policy is per tenant where that makes sense.** A retention period or a provider
preference is a tenant's decision on a multi-tenant platform; a global-only setting forces every
customer onto one answer.

**Config remains the default; runtime overrides it.** A service with no runtime value configured
behaves exactly as it does today, so nothing has to be migrated and a fresh deployment still boots
with zero database rows.

## What must never happen

1. **A stored secret readable through any API.** Not in a manifest, not in a config read, not in an
   error, not in a log line.
2. **A secret settable without a specific capability**, or without an audit record naming the actor.
3. **A rotation that silently breaks the provider** — verification is not optional.
4. **A KEK reachable from the UI.** The root of trust is deploy-time or it is not a root of trust.
5. **A driver swapped at runtime.** Wiring stays at boot: an adapter that can change under a live
   request is a class of bug nobody can reproduce.
6. **A runtime value that bypasses its boot guard.** If production refuses to start on a bad value,
   the same rule must reject it at runtime.

## Approach — service by service

One service per task, and each one is finished before the next starts: routes, declared admin
actions, audit entries, tests, gate run. A half-migrated service is worse than an unmigrated one,
because an operator cannot tell which settings are live.

- [x] **44.1** `SecretStore` core: envelope encryption, write-only access, masked hints, previous-value window
- [x] **44.2** `RuntimeConfig` core: typed per-tenant overrides, config fallback, the same validation as the boot guard
- [x] **44.3** The shared admin surface: set / rotate / verify / clear, `platform.secrets`, audited — declared once and copied per service, since services stay self-contained
- [x] **44.4** A gate: no route may return a stored secret, and every secret-setting route must declare its capability and audit
- [x] **44.5** **notification-service** — 17 credential knobs, the largest surface; provider preference and failover order
- [x] **44.6** **billing-service** — payment provider keys; the one where a wrong value moves money
- [x] **44.7** **zatca-service** — ZATCA credentials, needed after launch by definition
- [x] **44.8** **connector-service** — third-party OAuth secrets
- [x] **44.9** **stream-service** — recording sink and camera credentials
- [x] **44.10** **verification-service** — provider keys
- [x] **44.11** **assistant-service** — model and embeddings keys
- [x] **44.12** **file-service**, **webhook-service**, **compliance-service**, **identity-service** — remaining credential knobs
- [x] **44.13** Runtime policy values: retention, quotas, thresholds, grace periods — per service, per tenant
- [x] **44.14** Fill the empty admin catalogs (api-gateway, file-service) so every service declares its operator controls
- [x] **44.15** Full gate run, docs, and a rotation runbook

## Definition of done

- No API returns a stored secret — proven by a gate, not by review
- Every credential change is audited with an actor and without the value
- A rotation with a bad credential is refused and the working one survives
- A service with no runtime configuration behaves exactly as it does today
- Every service declares its operator controls; no empty admin catalog remains
- Mutation-tested: the write-only rule, the verification step and the capability check each break a
  test when removed

## Honest limits

- **This does not make the platform secure against a compromised admin account.** It narrows what
  such an account can read — a stolen panel session cannot exfiltrate existing keys — but it can
  still replace them. Audit is what makes that visible, and audit is a detection control, not a
  prevention one.
- **The KEK is a single point of failure.** Lose it and every stored credential is unrecoverable;
  they must be re-entered. That is the correct trade against the alternative, where the database
  alone is enough to steal them, but it needs to be in the runbook rather than discovered.
- **Verification proves a credential works now, not that it is the right one.** A valid key for the
  wrong account passes.
