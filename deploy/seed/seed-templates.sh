#!/usr/bin/env bash
# =============================================================================
# seed-templates.sh — register a tenant's WhatsApp template library and start it
# through Meta review, SERVICE-TO-SERVICE, with no user session and no signing key.
#
# THE WORKAROUND THIS REPLACES. Before the internal write routes existed, seeding a
# library meant reading identity-service's TOKEN_PRIVATE_KEY and MINTING a user JWT
# with caps:["notifications.templates"]. That works and is not acceptable as a
# product path: anything holding the signing key can impersonate ANY user on the
# platform. This script holds no key and mints nothing — it presents the shared
# X-Internal-Token to notification-service's internal twins:
#
#     POST /internal/whatsapp/templates                       (register — local upsert)
#     POST /internal/whatsapp/templates/submit-pending?tenant= (drain to Meta, 100/hr)
#
# register() is a local upsert (no Meta call, no rate limit), so the whole library
# is registered in one pass; it is idempotent and never un-approves an unchanged
# template. submit() hits Meta's 100/hour-per-WABA cap, so this submits ONE batch
# and leaves the rest to the hourly drain cron — it does not sleep for an hour.
#
#   NOTIFY_INTERNAL_URL=http://notification-service \
#   INTERNAL_SERVICE_TOKEN=*** \
#   TENANT=acme \
#   TEMPLATES_FILE=deploy/seed/templates.example.json \
#     deploy/seed/seed-templates.sh
#
#   deploy/seed/seed-templates.sh --no-submit   # register only; let the cron submit
#
# The library is the ADOPTER's — notification-service is content-agnostic (the body
# is opaque to it). TEMPLATES_FILE is a JSON array of:
#   { "name", "language", "category", "body", "variables": [...], "examples": [...] }
# =============================================================================
set -uo pipefail

die() { echo "ERROR: $*" >&2; exit 1; }

SUBMIT=1
for a in "${@:-}"; do
  case "$a" in
    --no-submit) SUBMIT=0 ;;
    "") : ;;
    *) die "unknown argument: $a" ;;
  esac
done

: "${NOTIFY_INTERNAL_URL:?set NOTIFY_INTERNAL_URL (base URL of notification-service, e.g. http://notification-service)}"
: "${INTERNAL_SERVICE_TOKEN:?set INTERNAL_SERVICE_TOKEN (the shared internal token — NEVER a user JWT, NEVER the signing key)}"
: "${TENANT:?set TENANT (the establishment this library belongs to)}"
: "${TEMPLATES_FILE:?set TEMPLATES_FILE (path to the JSON array of templates)}"
[ -f "$TEMPLATES_FILE" ] || die "TEMPLATES_FILE not found: $TEMPLATES_FILE"
command -v python3 >/dev/null 2>&1 || die "python3 is required"

echo "==> seeding WhatsApp templates for tenant '$TENANT' via $NOTIFY_INTERNAL_URL (internal token, no JWT)"

# All HTTP is driven from python's stdlib (urllib) so there is no jq dependency and
# the internal token is passed as a header, never interpolated into a shell string
# where it could land in a process list. The token is read from the environment
# INSIDE python; it is never echoed.
python3 - "$NOTIFY_INTERNAL_URL" "$TENANT" "$TEMPLATES_FILE" "$SUBMIT" <<'PY'
import json, os, sys, urllib.error, urllib.request

base, tenant, path, do_submit = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3], sys.argv[4] == "1"
token = os.environ["INTERNAL_SERVICE_TOKEN"]

def call(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
        headers={"X-Internal-Token": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return r.status, json.load(r)

try:
    templates = json.load(open(path, encoding="utf-8"))
except (OSError, ValueError) as exc:
    print(f"ERROR: cannot read {path}: {exc}", file=sys.stderr); sys.exit(1)
if not isinstance(templates, list):
    print("ERROR: TEMPLATES_FILE must be a JSON ARRAY of template objects", file=sys.stderr); sys.exit(1)

registered = failed = 0
for t in templates:
    name, lang = t.get("name", ""), t.get("language", "")
    payload = {"tenant": tenant, "name": name, "language": lang,
               "category": t.get("category", ""), "body": t.get("body", ""),
               "variables": t.get("variables", []), "examples": t.get("examples", [])}
    try:
        call("POST", f"{base}/internal/whatsapp/templates", payload)   # local upsert, idempotent
        registered += 1
    except urllib.error.HTTPError as e:
        failed += 1
        print(f"    register FAILED {name}/{lang}: HTTP {e.code} {e.read().decode()[:120]}", file=sys.stderr)
    except urllib.error.URLError as e:
        print(f"ERROR: cannot reach notification-service: {e}", file=sys.stderr); sys.exit(1)
print(f"==> registered {registered}/{len(templates)} template(s)" + (f", {failed} failed" if failed else ""))

if not do_submit:
    print("==> --no-submit: registered only. The hourly drain cron will submit them to Meta.")
    sys.exit(0)

try:
    _, res = call("POST", f"{base}/internal/whatsapp/templates/submit-pending?tenant={tenant}")
except urllib.error.HTTPError as e:
    print(f"ERROR: submit-pending failed: HTTP {e.code} {e.read().decode()[:200]}", file=sys.stderr); sys.exit(1)
_extra = f", {res['failed']} failed" if res.get("failed") else ""
print(f"==> submitted {res.get('submitted', 0)} to Meta this batch; {res.get('remaining', 0)} remaining{_extra}.")
if res.get("remaining", 0) > 0:
    print("    Meta rate-limits creation to 100/hour per WABA — the hourly drain cron "
          "(POST /internal/whatsapp/templates/submit-pending) will finish the rest.")
PY

echo "==> done. No user was impersonated and no signing key was read."
