# config/ — product identity

Platform-wide configuration. Not a service.

## What lives here

| file | purpose |
|---|---|
| `product.example.json` | The template. Committed. All placeholders, no real values. |
| `product.json` | Your filled copy. **Never committed** — same rule as `.env`. |
| `product.schema.json` | Shape validation. |

Validate with:

```bash
python scripts/check_product_config.py
```

Runs against `product.json` when it exists, otherwise the example — so a fresh clone passes.

## Why this file exists

A product gets renamed. It happens to almost everyone, and it happens late.

Without this file, the product's name is spread across service names, database names, table names,
JSON keys, log fields, headers, email templates, and PDF footers. Renaming becomes a
search-and-replace across every repository, and the parts that are missed are found by customers.

With it, a rename is one edit.

## The three levels — never mix them

| level | where | changes per |
|---|---|---|
| build-time identity | **this file** | never, ideally |
| runtime wiring | env vars | environment |
| white-label branding | `tenant-service` | tenant |

The product's name is build-time. A database URL is runtime. A customer's logo is per-tenant.
A value in the wrong level is a bug you find at the worst moment.

## The rule that matters

**`identifiers` is frozen.**

Those five values are not cosmetic. They are written into data your customers already hold, and
into artifacts you have already signed:

| identifier | what breaks if you change it |
|---|---|
| `jwtIssuer` | every live session and refresh token is rejected |
| `linkNs` | every share link ever sent stops resolving |
| `signatureDomain` | **every signed artifact fails verification, forever** |

A signed artifact is immutable. Its signature domain is immutable with it.

No schema catches this, because the new value is perfectly well-formed. No test catches it, because
tests sign and verify in the same process with the same config. Only production catches it, and by
then the damage is permanent.

**So: to rebrand, edit `identity` and `brand`. Leave `identifiers` alone.**

`scripts/check_product_config.py` compares `identifiers` against the previous commit and fails the
build unless the commit body contains a `MIGRATION:` line. Versioned identifiers (`linkNs`,
`signatureDomain`) may only move forward by bumping the suffix — `acme.link.v1` → `acme.link.v2` —
so old artifacts keep verifying against the old value.

## Service names carry no product name

`billing`, never `acme-billing`.

A rename must never touch a service name, a database name, a table, a JSON key, a log field, or a
header. The schema rejects a malformed name; the CI check rejects any name containing the product
slug — which a schema cannot see, because it does not know the slug.

## What the CI check enforces

Beyond the schema:

1. `locale.rtl` ⊆ `locale.supported`, and `locale.default` is in it.
   Otherwise the UI flips right-to-left for a language the product does not ship.
2. Every bilingual block carries exactly the locales in `locale.supported`.
   A missing `ar` is not caught by a schema. It is caught by a customer.
3. `identifiers` unchanged since the previous commit, unless the commit says `MIGRATION:`.
4. Versioned identifiers move by `+1` on the same stem, never by editing in place.
5. No service name contains the product slug.
6. In a real `product.json` (not the example): no empty required value, and no leftover
   `example.com` URL.

Zero runtime dependencies. `jsonschema` is used when installed and skipped when not, so the rules
that matter still run on a bare box.
