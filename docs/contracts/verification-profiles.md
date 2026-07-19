# Wiring a verification provider

Every provider is **config**, not code. Nafath, Wathq, Companies House, VIES, BankID or one we have
never heard of are all the same adapter with a different profile.

> **These profiles are templates, not contracts.** We do not hold verified API documentation for
> Nafath or Wathq. The field names and URLs below are placeholders shaped like the real thing —
> replace them with the contract *you* hold. Shipping invented endpoints would produce a file that
> looks authoritative, passes every test written against our own invention, and fails the first
> time it meets the real API.

---

## The five things a profile answers

| key | question |
|---|---|
| `url` | where to POST |
| `auth` | `bearer:SECRET_NAME` or `header:X-Api-Key:SECRET_NAME` |
| `request` | what to send — `{claim}` is the identifier |
| `verdict_path` | **where the answer lives** in the response |
| `verdict_ok` / `verdict_refused` / `verdict_pending` | which values mean what |

`verdict_path` is the load-bearing one. Get it wrong and every response reads as `error` — which is
safe but useless. There is deliberately no way to get it wrong *in the other direction*: a value
nobody configured is `error`, never `verified`.

## A business registry (Wathq-shaped)

```bash
VERIFY_PROVIDERS="business=wathq"
VERIFY_PROFILES="wathq.url=https://api.example.sa/v1/commercial-registration,\
wathq.auth=bearer:WATHQ_TOKEN,\
wathq.request=crNumber={claim},\
wathq.verdict_path=data.status,\
wathq.verdict_ok=ACTIVE,\
wathq.verdict_refused=NOT_FOUND,CANCELLED,SUSPENDED,\
wathq.reference_path=data.requestId"
VERIFY_SECRETS="wathq=WATHQ_TOKEN"
VERIFY_TTL_DAYS="business=365"
```

`CANCELLED` and `SUSPENDED` are listed as **refusals**, not errors — the registry answered, and the
answer is that this company may not trade. A TTL matters here: a registration active today can be
cancelled next month, so "verified once" is not "verified forever".

## An identity provider with a phone prompt (Nafath-shaped)

```bash
VERIFY_PROVIDERS="individual=nafath"
VERIFY_PROFILES="nafath.url=https://api.example.sa/v1/verify,\
nafath.auth=bearer:NAFATH_TOKEN,\
nafath.request=nationalId={claim}&purpose=onboarding,\
nafath.verdict_path=status,\
nafath.verdict_ok=COMPLETED,\
nafath.verdict_refused=REJECTED,EXPIRED,\
nafath.verdict_pending=WAITING_USER,\
nafath.reference_path=transId"
VERIFY_SECRETS="nafath=NAFATH_TOKEN"
VERIFY_PENDING_TTL_SECONDS=900
```

`WAITING_USER` is `pending`: the person approves on their phone and the provider calls back. A
prompt nobody answers **lapses** — and lapsing is not approval.

## No provider at all

```bash
VERIFY_PROVIDERS="business=manual,individual=manual"
VERIFY_TWO_EYES=true
```

Someone uploads a certificate, a reviewer decides. Works in any market on day one, needs no
contract with anybody, and is the highest-privilege operation in the service — so both approvals
and refusals are audited, and a reviewer can never decide their own claim.

---

## What the platform refuses to do

- **Boot in production with a `null` verifier** for a claim type it says it verifies.
- **Boot with a provider that has no `url` or no `verdict_path`** — without the second we would not
  know where the answer lives, so every check would read as error while the dashboard showed the
  feature enabled.
- **Treat an unknown as a yes.** Missing field, unreadable body, HTTP error, unconfigured value:
  all `error`.
- **Confuse `refused` with `error`.** A 500 from a registry does not mean the company is
  unregistered. Merging them means either locking out every customer during an outage, or admitting
  the person the check exists to stop.
- **Store your raw identifiers.** Only a salted one-way hash and the verdict. The store cannot leak
  identities even if read in full.
- **Accept a raw identifier on the lookup endpoint.** `identity/{claim_ref}/seen` takes the hash, so
  it cannot be used to ask whether a given national id exists in our records.
