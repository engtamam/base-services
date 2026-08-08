#!/usr/bin/env bash
# ==================================================================================================
#  deploy/hooks/post-deploy.sh — runs AFTER the rollout (setup.sh phase 7).
#
#  The plug-and-play seam for anything that must run once the services are up: seed reference data,
#  register a first operator, warm a cache. base-services ships this as a NO-OP (it self-provisions
#  its own schema on boot and needs no seed); a product replaces or extends it without touching
#  setup.sh.
#
#  Example:
#    HERE="$(cd "$(dirname "$0")" && pwd)"
#    python3 "$HERE/../seed/seed_operator.py"
# ==================================================================================================
set -Eeuo pipefail
echo "▸ post-deploy: nothing to do (base-services self-provisions). Add product seeds here."
