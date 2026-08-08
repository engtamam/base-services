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

# which changed since PREV (path-scoped to each service dir)
changed() {
  [ "$REBUILD_ALL" = 1 ] && { echo "$SVCS"; return; }
  for s in $SVCS; do
    if [ -n "$(git diff --name-only "$PREV..HEAD" -- "services/$s" 2>/dev/null)" ]; then echo "$s"; fi
  done
}
CHANGED="$(changed)"

echo "==> publishing $VERSION to $REG   (prev=${PREV:-<all>}, HEAD=$(git rev-parse --short HEAD))"
echo "==> changed since ${PREV:-<all>}: ${CHANGED:-<none>}"

# Build/retag one service, then move its :latest. Independent per service, so the loop below runs
# these in parallel. A failure drops a marker file rather than a shared variable, because each
# invocation is its own subshell and a `fail=1` there would not survive back to the parent.
FAIL_MARK="$(mktemp)"; rm -f "$FAIL_MARK"
publish_one() {
  s="$1"
  img="$REG/$s:$VERSION"
  if echo "$CHANGED" | grep -qx "$s"; then
    echo "==> build+push $s"
    if ! docker buildx build --platform linux/amd64 \
          --label "org.opencontainers.image.source=https://github.com/$OWNER/$(basename "$ROOT")" \
          --label "org.opencontainers.image.revision=$SHA" \
          --label "org.opencontainers.image.version=$VERSION" \
          --label "org.opencontainers.image.licenses=Proprietary" \
          -t "$img" --push "services/$s" >/tmp/pl_$s.log 2>&1; then
      echo "   FAIL build $s (see /tmp/pl_$s.log)"; : > "$FAIL_MARK"; return 1
    fi
  else
    # unchanged: copy the previous release's manifest to the new tag — no rebuild
    if ! docker buildx imagetools create --tag "$img" "$REG/$s:$PREV" >/tmp/pl_$s.log 2>&1; then
      echo "   FAIL retag $s (see /tmp/pl_$s.log)"; : > "$FAIL_MARK"; return 1
    fi
  fi
  # move :latest onto the new version
  docker buildx imagetools create --tag "$REG/$s:latest" "$img" >/dev/null 2>&1 \
    || { echo "   WARN could not move :latest for $s"; }
}
export -f publish_one
export REG VERSION PREV SHA OWNER ROOT CHANGED FAIL_MARK MSYS_NO_PATHCONV

# Bounded parallelism. Docker itself parallelizes layers WITHIN a build, so building all services at
# once would just thrash a small builder (CPU/RAM/disk) and finish no sooner — often later. Default
# to 4; override with PL_JOBS. Each build's own log is /tmp/pl_<svc>.log.
JOBS="${PL_JOBS:-4}"
echo "==> building/retagging with up to $JOBS in parallel"
echo "$SVCS" | xargs -P "$JOBS" -I{} bash -c 'publish_one "$@"' _ {}
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
