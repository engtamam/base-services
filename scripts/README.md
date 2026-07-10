# base-services — tooling

## `create_service.py` — smart service generator

Clones `services/_template` into a new **100% self-contained** service, picks the
ports/adapters you want, then **self-verifies** (runs the new service's `pytest`
+ security gate) and only commits on green. Atomic: builds in a temp dir, moves
into place on success, **rolls back** on any failure. Stdlib only.

### Usage

```bash
# simplest — all template defaults, auto-assigned port, self-verified
python scripts/create_service.py auth-service --desc "Auth & sessions"

# pick adapters per port
python scripts/create_service.py files --object-store s3 --cache redis

# preview without writing anything
python scripts/create_service.py my-svc --dry-run

# from a recorded spec (flags still override)
python scripts/create_service.py --spec my.spec.toml
```

### Flags

| Flag | Meaning |
|------|---------|
| `name` | service name (kebab-case; validated, no path traversal) |
| `--desc` | description |
| `--port` | port (auto-assigned from `services/.registry.json` if omitted) |
| `--datastore/--cache/--event-bus/--queue/--object-store/--secrets/--token-verifier/--rate-limiter` | driver per port (validated against the allowlist) |
| `--spec FILE` | read `service.spec.toml` (CLI flags override) |
| `--python PATH` | interpreter used for verification (default: template `.venv`) |
| `--dry-run` | print the plan, write nothing |
| `--force` | overwrite an existing service |
| `--no-verify` | skip pytest + security gate |

### What it writes

- `services/<name>/` — the service (package stays `app/`; only metadata renamed).
- `.env` — runtime config with a fresh `INTERNAL_SERVICE_TOKEN` + **safe** chosen
  drivers activated; infra drivers (postgres/redis/s3/…) are recorded but left
  commented (they need connection/keys), so the service still boots zero-infra.
- `service.spec.toml` — the record of choices (reproducible).
- updates `services/.registry.json` (port map).

### Verification

Runs the generated service's `pytest` + `ci/security_gate.py`. If either fails,
the generation is **rolled back** and nothing is written. Verification reuses the
template's `.venv` by default (deps are identical) so it's fast.

Run the generator's own tests: `python -m pytest scripts/tests`.
