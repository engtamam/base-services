#!/usr/bin/env bash
# =============================================================================
# publish_local.sh — cut a release WITHOUT GitHub Actions: build the changed
# services here, push them to GHCR, re-tag the unchanged ones from the previous
# release, move :latest, and verify every digest.
#
# This is the local counterpart to release.sh + release.yml, for when the CI
# build is unavailable (e.g. an Actions billing block). It does exactly what we
# do by hand: rebuild only what changed, re-tag the rest (no wasted rebuilds),
# and point :latest at the new version.
#
#   scripts/publish_local.sh v0.11.3            # prev = the newest tag before HEAD
#   scripts/publish_local.sh v0.11.3 v0.11.2    # prev given explicitly
#   scripts/publish_local.sh v0.11.3 --all      # rebuild ALL services, not just changed
#
# It does NOT create the git tag — cut that yourself once you are happy:
#   git tag -a v0.11.3 -m v0.11.3 && git push origin v0.11.3
# Deploys pin by DIGEST, not :latest; :latest is moved only as a convenience.
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export MSYS_NO_PATHCONV=1          # Windows/Git-Bash: keep image refs from being path-mangled

die() { echo "ERROR: $*" >&2; exit 1; }

VERSION="${1:-}"
[ -n "$VERSION" ] || die "usage: scripts/publish_local.sh vX.Y.Z [vPREV|--all]"
echo "$VERSION" | grep -qE '^v[0-9]+\.[0-9]+\.[0-9]+$' || die "version must look like v1.2.3"

ARG2="${2:-}"
REBUILD_ALL=0
PREV=""
if [ "$ARG2" = "--all" ]; then
  REBUILD_ALL=1
elif [ -n "$ARG2" ]; then
  PREV="$ARG2"
else
  # newest semver tag that is an ancestor of HEAD and not VERSION itself
  PREV="$(git tag --merged HEAD 2>/dev/null | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' \
          | grep -vx "$VERSION" | sort -V | tail -1)"
fi
[ "$REBUILD_ALL" = 1 ] || [ -n "$PREV" ] || die "no previous tag found — pass one explicitly or use --all"

# owner/registry, owner lowercased exactly as the workflow does
REMOTE_URL="$(git remote get-url origin)"
OWNER="$(echo "$REMOTE_URL" | sed -E 's#(git@github.com:|https://github.com/)##; s#/.*##' | tr '[:upper:]' '[:lower:]')"
REG="ghcr.io/$OWNER"
[ -n "$OWNER" ] || die "could not parse owner from $REMOTE_URL"
SHA="$(git rev-parse HEAD)"

# the 22 services: every Dockerfile dir except the _template seed
SVCS="$(find services -maxdepth 2 -name Dockerfile -printf '%h\n' | sed 's#services/##' | grep -v '^_template$' | sort)"

# which changed since PREV — scoped to the paths that ACTUALLY go into the image, not the whole
# service dir. Every Dockerfile copies exactly app/, pyproject.toml and README.md (plus the
# Dockerfile itself governs the build), so a change under ci/ or tests/ produces a byte-identical
# image: rebuilding it is a cache-hit that wastes the build lane where a re-tag would do. Scoping
# here lets a fleet-wide ci/ change (a gate fix touching all 24) re-tag the unchanged 22 instead of
# rebuilding them. A missed rebuild is worse than a redundant one, so this list must stay a
# SUPERSET of what the Dockerfile reads — widen it the day a Dockerfile copies something new.
IMAGE_PATHS="app pyproject.toml README.md Dockerfile"
changed() {
  [ "$REBUILD_ALL" = 1 ] && { echo "$SVCS"; return; }
  for s in $SVCS; do
    paths=""; for p in $IMAGE_PATHS; do paths="$paths services/$s/$p"; done
    if [ -n "$(git diff --name-only "$PREV..HEAD" -- $paths 2>/dev/null)" ]; then echo "$s"; fi
  done
}
CHANGED="$(changed)"

echo "==> publishing $VERSION to $REG   (prev=${PREV:-<all>}, HEAD=$(git rev-parse --short HEAD))"
echo "==> changed since ${PREV:-<all>}: ${CHANGED:-<none>}"

# Build/retag one service, then move its :latest. Independent per service, so the loop below runs
# these in parallel. A failure drops a marker file rather than a shared variable, because each
# invocation is its own subshell and a `fail=1` there would not survive back to the parent.
FAIL_MARK="$(mktemp)"; rm -f "$FAIL_MARK"
build_push() {   # build+push one service by digest; log to /tmp/pl_$1.log
  s="$1"; img="$2"
  docker buildx build --platform linux/amd64 \
    --label "org.opencontainers.image.source=https://github.com/$OWNER/$(basename "$ROOT")" \
    --label "org.opencontainers.image.revision=$SHA" \
    --label "org.opencontainers.image.version=$VERSION" \
    --label "org.opencontainers.image.licenses=Proprietary" \
    -t "$img" --push "services/$s" >/tmp/pl_$s.log 2>&1
}
export -f build_push

publish_one() {
  s="$1"
  img="$REG/$s:$VERSION"
  if echo "$CHANGED" | grep -qx "$s"; then
    echo "==> build+push $s"
    build_push "$s" "$img" || { echo "   FAIL build $s (see /tmp/pl_$s.log)"; : > "$FAIL_MARK"; return 1; }
  else
    # unchanged: copy the previous release's manifest to the new tag — no rebuild. A re-tag is an
    # OPTIMISATION, never a requirement: if it cannot run (a GHCR permission quirk on that package, a
    # missing PREV image) FALL BACK to a full rebuild rather than fail the release. The image is
    # identical either way; the digest verify below is the single source of truth for correctness.
    if ! docker buildx imagetools create --tag "$img" "$REG/$s:$PREV" >/tmp/pl_$s.log 2>&1; then
      echo "   retag failed for $s — rebuilding instead"
      build_push "$s" "$img" || { echo "   FAIL build(after retag) $s (see /tmp/pl_$s.log)"; : > "$FAIL_MARK"; return 1; }
    fi
  fi
  # move :latest onto the new version
  docker buildx imagetools create --tag "$REG/$s:latest" "$img" >/dev/null 2>&1 \
    || { echo "   WARN could not move :latest for $s"; }
}
export -f publish_one
export REG VERSION PREV SHA OWNER ROOT CHANGED FAIL_MARK MSYS_NO_PATHCONV

# TWO budgets, because a build and a re-tag cost wildly different things.
#
# A BUILD is CPU/RAM/disk-bound. Docker already parallelizes layers WITHIN a build, so running many
# at once just thrashes a small builder and finishes no sooner — often later. Bounded, default 10,
# override with PL_JOBS.
#
# A RE-TAG is a registry manifest copy (`imagetools create`): no image data moves, no CPU, just a
# small API call. Throttling those to the build budget was pure waste — on a release that changed one
# service, twenty-two near-instant re-tags queued four-at-a-time behind nothing. They get their own,
# much higher budget (PL_RETAG_JOBS, default 24 ≈ all-at-once) and run CONCURRENTLY with the builds,
# so total time is ~the build time and the re-tags are effectively free.
JOBS="${PL_JOBS:-10}"
RETAG_JOBS="${PL_RETAG_JOBS:-24}"
CHANGED_SVCS=""; UNCHANGED_SVCS=""
for s in $SVCS; do
  if echo "$CHANGED" | grep -qx "$s"; then CHANGED_SVCS="$CHANGED_SVCS $s"
  else UNCHANGED_SVCS="$UNCHANGED_SVCS $s"; fi
done
n_changed="$(set -- $CHANGED_SVCS; echo $#)"; n_unchanged="$(set -- $UNCHANGED_SVCS; echo $#)"
echo "==> building $n_changed changed (<= $JOBS parallel) + re-tagging $n_unchanged unchanged (<= $RETAG_JOBS parallel), concurrently"
# Kick the cheap re-tags off at high parallelism in the BACKGROUND, build the changed ones at the
# CPU-bound budget in the foreground, then wait for the re-tags. `|| true` on each: FAIL_MARK and the
# digest verify below are the single source of truth for success, so we always reach the summary.
retag_pid=""
if [ -n "${UNCHANGED_SVCS// }" ]; then
  printf '%s\n' $UNCHANGED_SVCS | xargs -P "$RETAG_JOBS" -I{} bash -c 'publish_one "$@"' _ {} &
  retag_pid=$!
fi
if [ -n "${CHANGED_SVCS// }" ]; then
  printf '%s\n' $CHANGED_SVCS | xargs -P "$JOBS" -I{} bash -c 'publish_one "$@"' _ {} || true
fi
[ -n "$retag_pid" ] && { wait "$retag_pid" || true; }
fail=0; [ -f "$FAIL_MARK" ] && fail=1; rm -f "$FAIL_MARK"

echo "==> verifying every service has a $VERSION digest"
missing=0
for s in $SVCS; do
  d="$(docker buildx imagetools inspect "$REG/$s:$VERSION" 2>/dev/null | awk '/^Digest:/{print $2; exit}')"
  printf '    %-26s %s\n' "$s" "${d:-MISSING}"
  [ -n "$d" ] || missing=1
done

N="$(echo "$SVCS" | wc -w | tr -d ' ')"
[ "$fail" = 0 ] && [ "$missing" = 0 ] \
  && echo "==> $VERSION published: $N/$N on $REG, :latest moved. Now cut the tag when ready." \
  || { echo "==> INCOMPLETE — some services failed or are missing above"; exit 1; }
