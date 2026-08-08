#!/usr/bin/env bash
# ==================================================================================================
#  deploy/hooks/pre-deploy.sh — runs BEFORE the rollout (setup.sh phase 5).
#
#  The plug-and-play seam for anything that must exist before the services start: secrets, DB roles,
#  migrations. Ships doing the one thing every install needs — minting the platform secrets — and a
#  product extends it (or replaces it) without touching setup.sh.
#
#  Default: generate + apply the platform secrets from secrets.schema.json (idempotent; preserves any
#  values already in the cluster so a re-run never drifts a password off the persisted data).
# ==================================================================================================
set -Eeuo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SERVER="$(cd "$HERE/../server" && pwd)"

if [ -f "$SERVER/gen-secrets.py" ]; then
  echo "▸ minting platform secrets (gen-secrets.py)"
  python3 "$SERVER/gen-secrets.py"
fi

# Add product-specific pre-deploy steps below (DB roles/migrations, a seed of reference data, ...).
# Example:
#   [ -f "$SERVER/provision-db.sh" ] && bash "$SERVER/provision-db.sh"
