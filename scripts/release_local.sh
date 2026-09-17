#!/usr/bin/env bash
# =============================================================================
# release_local.sh — cut a COMPLETE release on this machine, no GitHub Actions.
#
# This is the local twin of release.yml. When Actions is unavailable (an account
# billing block, an outage), one command still does EVERYTHING the pipeline does,
# in the same order, with the same gates:
#
#   1. build + push       every changed service to GHCR by digest, re-tag the
#                         unchanged ones from the previous release, move :latest
#                         — delegated to publish_local.sh (no wasted rebuilds).
#   2. CVE gate  (TEST)   trivy scans every published image for HIGH/CRITICAL,
#                         --ignore-unfixed. A vulnerable image is NEVER released.
#   3. pin digests        turn the published digests into deploy/pinned values.
#   4. render             prove the chart renders against the REAL digests.
#   5. package            deploy-pinned-<ver>.tar.gz + checksums.txt + digests.json
#   6. tag                create + push the annotated git tag (skip with --no-tag).
#   7. publish            create the GitHub Release and upload the artifacts.
#
# A release is not "images were pushed" — it is "these exact digests, scanned
# clean, the chart renders with them, and a Release records them". Every step is
# a gate: any failure stops the release before the tag or the Release exist.
#
#   scripts/release_local.sh v0.16.5              # prev = newest tag before HEAD
#   scripts/release_local.sh v0.16.5 v0.16.4      # prev given explicitly
#   scripts/release_local.sh v0.16.5 --all        # rebuild ALL, not just changed
#   scripts/release_local.sh v0.16.5 --no-tag     # do everything but the git tag
#   scripts/release_local.sh v0.16.5 --scan       # ALSO run the local CVE gate (off by default)
#
# Auth: GHCR push uses your `docker login ghcr.io` (needs write:packages). The
# GitHub Release uses `gh` if present, else $GH_TOKEN/$GITHUB_TOKEN, else the
# token git already stored for github.com. Tokens are never printed.
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export MSYS_NO_PATHCONV=1          # Windows/Git-Bash: keep image refs from being path-mangled

die() { echo "ERROR: $*" >&2; exit 1; }

# ── args ────────────────────────────────────────────────────────────────────
VERSION="${1:-}"
[ -n "$VERSION" ] || die "usage: scripts/release_local.sh vX.Y.Z [vPREV|--all] [--no-tag] [--scan]"
echo "$VERSION" | grep -qE '^v[0-9]+\.[0-9]+\.[0-9]+$' || die "version must look like v1.2.3, got: $VERSION"

# The CVE gate is OFF by default and runs only on request (--scan). The images this pipeline ships
# are already CVE-scanned by the release CI on every push, so re-scanning here — which after the
# post-release prune re-pulls each image — is redundant on a routine local cut. Pass --scan to run it
# (before a real customer-facing launch, or when CI is unavailable). --no-scan stays accepted as a
# no-op so old muscle memory does not error.
PREV_ARG=""; DO_TAG=1; DO_SCAN=0
shift
for a in "$@"; do
  case "$a" in
    --no-tag)  DO_TAG=0 ;;
    --scan)    DO_SCAN=1 ;;
    --no-scan) DO_SCAN=0 ;;
    --all)     PREV_ARG="--all" ;;
    v[0-9]*)   PREV_ARG="$a" ;;
    *)         die "unknown argument: $a" ;;
  esac
done

# ── owner / repo, exactly as the workflow derives them ──────────────────────
REMOTE_URL="$(git remote get-url origin)"
OWNER="$(echo "$REMOTE_URL" | sed -E 's#(git@github.com:|https://github.com/)##; s#/.*##' | tr '[:upper:]' '[:lower:]')"
REPO="$(echo "$REMOTE_URL"  | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')"
REG="ghcr.io/$OWNER"
[ -n "$OWNER" ] && [ -n "$REPO" ] || die "could not parse owner/repo from $REMOTE_URL"

# The services: every Dockerfile dir except the _template seed.
SVCS="$(find services -maxdepth 2 -name Dockerfile -printf '%h\n' | sed 's#services/##' | grep -v '^_template$' | sort)"
N="$(echo "$SVCS" | wc -w | tr -d ' ')"

# The previous release tag, used ONLY to skip re-scanning unchanged images: a re-tagged service
# carries the EXACT digest of the previous release, which already passed the CVE gate there, so
# scanning it again is a wasted pull + scan (and after the post-release image prune, a full re-pull
# of the whole fleet). The skip is by DIGEST EQUALITY, not a path heuristic — if a rebuild changed an
# image, its digest differs and it IS scanned. No prev, or --all, means scan everything.
SCAN_ALL=0
PREV_TAG=""
if [ "$PREV_ARG" = "--all" ]; then
  SCAN_ALL=1
elif [ -n "$PREV_ARG" ]; then
  PREV_TAG="$PREV_ARG"
else
  PREV_TAG="$(git tag --merged HEAD 2>/dev/null | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' \
              | grep -vx "$VERSION" | sort -V | tail -1)"
fi
[ -n "$PREV_TAG" ] || SCAN_ALL=1

echo "======================================================================"
echo " release_local  $VERSION   ->  $REG   ($N services)"
echo " prev=${PREV_ARG:-<auto>}  tag=$([ $DO_TAG = 1 ] && echo yes || echo no)  scan=$([ $DO_SCAN = 1 ] && echo yes || echo no)"
echo "======================================================================"

# ── 1. build + push (delegated) ─────────────────────────────────────────────
# publish_local.sh builds the changed services, re-tags the rest from the prev
# release, moves :latest, and verifies every service has a $VERSION digest.
echo; echo "==> [1/7] build + push + re-tag  (publish_local.sh)"
bash scripts/publish_local.sh "$VERSION" ${PREV_ARG:+$PREV_ARG} || die "build/push failed — nothing tagged"

# ── assemble the digest map from what is now on GHCR ────────────────────────
echo; echo "==> assembling digests.json from GHCR"
DIGJSON=""
for s in $SVCS; do
  d="$(docker buildx imagetools inspect "$REG/$s:$VERSION" 2>/dev/null | awk '/^Digest:/{print $2; exit}')"
  [ -n "$d" ] || die "no digest for $s on GHCR — the push did not complete"
  DIGJSON="$DIGJSON$s $d
"
done
printf '%s' "$DIGJSON" | python -c "
import json,sys
m={p.split()[0]:p.split()[1] for p in sys.stdin if len(p.split())==2}
open('digests.json','w').write(json.dumps(m,indent=2)+'\n')
print(f'    {len(m)} digests recorded')
"

# ── 2. CVE gate — a vulnerable image is never released ──────────────────────
# Runs trivy from its official image against the LOCAL docker daemon: we pull
# each image (your GHCR creds), then scan it via the mounted socket with a CLEAN
# docker config — otherwise a `credsStore` in ~/.docker/config.json makes trivy
# look for a credential helper that does not exist inside the container and the
# vuln-DB download itself fails. The DB is cached in a named volume so only the
# first scan downloads it.
if [ "$DO_SCAN" = 1 ]; then
  echo; echo "==> [2/7] CVE gate — trivy HIGH,CRITICAL (--ignore-unfixed)$([ "$SCAN_ALL" = 1 ] && echo " on all $N images" || echo ", changed images only (unchanged already scanned at $PREV_TAG)")"
  command -v docker >/dev/null || die "docker is required for the CVE gate (--scan); omit it to skip"
  EMPTY_CFG="$(mktemp -d)"; echo '{}' > "$EMPTY_CFG/config.json"
  docker pull -q aquasec/trivy:latest >/dev/null 2>&1 || true
  vulns=0; scanned=0; skipped=0
  for s in $SVCS; do
    img="$REG/$s:$VERSION"
    # Skip when this release's digest is byte-identical to the previous release's (a re-tag): it was
    # already CVE-scanned there. Digest equality is the truth — a rebuilt image differs and is scanned.
    if [ "$SCAN_ALL" != 1 ]; then
      cur="$(docker buildx imagetools inspect "$img" 2>/dev/null | awk '/^Digest:/{print $2; exit}')"
      prev="$(docker buildx imagetools inspect "$REG/$s:$PREV_TAG" 2>/dev/null | awk '/^Digest:/{print $2; exit}')"
      if [ -n "$cur" ] && [ "$cur" = "$prev" ]; then
        printf '    %-26s skip (unchanged since %s)\n' "$s" "$PREV_TAG"; skipped=$((skipped+1)); continue
      fi
    fi
    docker pull -q "$img" >/dev/null 2>&1 || die "could not pull $img to scan it"
    scanned=$((scanned+1))
    if DOCKER_CONFIG="$EMPTY_CFG" docker run --rm \
         -v //var/run/docker.sock:/var/run/docker.sock \
         -v trivy-db-cache:/root/.cache/trivy \
         aquasec/trivy:latest image --image-src docker --quiet \
         --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 "$img" \
         >/tmp/trivy_$s.log 2>&1; then
      printf '    %-26s clean\n' "$s"
    else
      printf '    %-26s VULNERABLE — see /tmp/trivy_%s.log\n' "$s" "$s"; vulns=1
    fi
  done
  [ "$vulns" = 0 ] || die "CVE gate FAILED — a HIGH/CRITICAL fixable vulnerability is present. Not releasing."
else
  echo; echo "==> [2/7] CVE gate SKIPPED (default; pass --scan to run it) — images are CVE-scanned by release CI"
fi

# ── 3. pin the digests into deploy values ───────────────────────────────────
echo; echo "==> [3/7] pin digests into deploy/pinned"
python tools/release/pin_digests.py --digests digests.json --registry "$REG" --out deploy/pinned || die "pinning failed"
python tools/release/pin_digests.py --verify deploy/pinned || die "something is left unpinned"

# ── 4. prove the chart renders with the real digests ────────────────────────
echo; echo "==> [4/7] prove the chart renders with the real digests"
command -v helm >/dev/null || die "helm is required to prove the chart renders"
for f in deploy/pinned/*.values.yaml; do
  n="$(basename "$f" .values.yaml)"
  helm template "$n" deploy/chart -f "$f" >/dev/null || die "chart FAILED to render for $n"
done
echo "    all $N values files render"

# ── 5. package the release artifact ─────────────────────────────────────────
echo; echo "==> [5/7] package the release artifact"
cp LICENSE deploy/pinned/LICENSE
TARBALL="deploy-pinned-${VERSION}.tar.gz"
tar -czf "$TARBALL" -C deploy pinned chart || die "packaging failed"
sha256sum "$TARBALL" digests.json > checksums.txt
echo "    $TARBALL + checksums.txt"

# ── 6. tag ──────────────────────────────────────────────────────────────────
# The tag is cut AFTER the images exist and pass the gate — never before. A tag
# that points at a release that never published is the exact failure release.yml
# guards against.
if [ "$DO_TAG" = 1 ]; then
  echo; echo "==> [6/7] tag $VERSION"
  if git rev-parse -q --verify "refs/tags/$VERSION" >/dev/null; then
    echo "    tag already exists locally — leaving it as-is"
  else
    git tag -a "$VERSION" -m "$(printf '%s\n\nReleased locally: images built+pushed, CVE-scanned, digests pinned and chart-rendered.' "$VERSION")" \
      || die "git tag failed"
  fi
  # Push the tag — a FAILED push MUST be fatal. The tag-publish incident was exactly this line
  # swallowing the error: the push was rejected (the PAT lacked the `workflow` scope and the tag
  # bridged `.github/workflows/ci.yml` edits) but `2>/dev/null ... || echo` hid it, the release
  # "succeeded", and step 7's `gh release create` then minted a LIGHTWEIGHT tag at the default
  # branch's (stale) HEAD — v0.25.2/v0.25.3 ended up on a 4-month-old wrong commit. Never again.
  # Distinguish "already on origin at the SAME commit" (fine) from any real failure or a silent move.
  local_sha="$(git rev-parse "$VERSION^{}")"
  remote_sha="$(git ls-remote origin "refs/tags/$VERSION^{}" | awk '{print $1}')"
  [ -z "$remote_sha" ] && remote_sha="$(git ls-remote origin "refs/tags/$VERSION" | awk '{print $1}')"
  if [ -n "$remote_sha" ]; then
    [ "$remote_sha" = "$local_sha" ] \
      && echo "    tag already on origin at the correct commit ($local_sha) — ok" \
      || die "tag $VERSION is on origin at $remote_sha but locally points at $local_sha — refusing to move a published tag silently; resolve by hand"
  else
    git push origin "refs/tags/$VERSION" \
      || die "pushing tag $VERSION FAILED (error above). Common cause: the PAT lacks the 'workflow' scope while this tag includes .github/workflows changes. Fix the token and re-run — do NOT let the release continue with an unpushed tag."
    echo "    pushed tag to origin"
  fi
else
  echo; echo "==> [6/7] tag SKIPPED (--no-tag)"
fi

# ── 7. publish the GitHub Release ───────────────────────────────────────────
echo; echo "==> [7/7] publish the GitHub Release"
BODY="$(cat <<EOF
**Proprietary software. All rights reserved. See LICENSE — no licence is granted.**

Container images (GHCR, private unless the repository is public):
\`$REG/<service>@<digest>\`

\`$TARBALL\` contains the chart and the values files pinned to the exact digests
published by this release. Deploy with:

\`\`\`bash
tar xzf $TARBALL
helm upgrade --install wallet-service ./chart -f pinned/wallet-service.values.yaml
\`\`\`

Cut locally (build+push, CVE gate, digest-pin, chart-render) — identical images
and digests to the CI pipeline.
EOF
)"

if command -v gh >/dev/null 2>&1; then
  if gh release view "$VERSION" >/dev/null 2>&1; then
    echo "    release exists — uploading/overwriting assets"
    gh release upload "$VERSION" "$TARBALL" digests.json checksums.txt --clobber || die "asset upload failed"
  else
    # --target pins the Release (and the tag it would create) to the EXACT commit. Without it,
    # if the tag were somehow absent on origin, gh would create a lightweight tag at the default
    # branch's HEAD — the second half of the wrong-tag incident. Step 6 already guarantees the tag
    # is on origin at $VERSION^{}; --target is belt-and-suspenders that costs nothing.
    gh release create "$VERSION" "$TARBALL" digests.json checksums.txt \
      --title "$VERSION" --notes "$BODY" --target "$(git rev-parse "$VERSION^{}")" || die "gh release create failed"
  fi
  echo "==> published: $(gh release view "$VERSION" --json url -q .url 2>/dev/null)"
else
  TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
  [ -n "$TOKEN" ] || TOKEN="$(printf 'protocol=https\nhost=github.com\n\n' | git credential fill 2>/dev/null | sed -n 's/^password=//p')"
  [ -n "$TOKEN" ] || die "no GitHub token (install gh, or set GH_TOKEN)"
  TARGET_SHA="$(git rev-parse "$VERSION^{}")"
  RID="$(python - "$TOKEN" "$REPO" "$VERSION" "$BODY" "$TARGET_SHA" <<'PY'
import sys,json,urllib.request,urllib.error
token,repo,ver,body,target=sys.argv[1:6]
def call(method,url,data=None):
    req=urllib.request.Request(url,data=(json.dumps(data).encode() if data else None),method=method,
        headers={"Authorization":f"Bearer {token}","Accept":"application/vnd.github+json","Content-Type":"application/json"})
    return json.load(urllib.request.urlopen(req))
try:
    r=call("GET",f"https://api.github.com/repos/{repo}/releases/tags/{ver}")
except urllib.error.HTTPError:
    # target_commitish pins the Release to the EXACT commit. Without it GitHub creates the tag at
    # the default branch's HEAD if it is absent — how v0.25.2/v0.25.3 landed on a stale commit.
    r=call("POST",f"https://api.github.com/repos/{repo}/releases",
           {"tag_name":ver,"target_commitish":target,"name":ver,"body":body,"generate_release_notes":True})
print(r["id"])
PY
)" || die "creating the Release failed"
  UP="https://uploads.github.com/repos/$REPO/releases/$RID/assets"
  for a in "$TARBALL:application/gzip" "digests.json:application/json" "checksums.txt:text/plain"; do
    f="${a%%:*}"; ct="${a##*:}"
    curl -s -o /dev/null -w "    $f -> HTTP %{http_code}\n" -X POST \
      -H "Authorization: Bearer $TOKEN" -H "Content-Type: $ct" \
      --data-binary @"$f" "$UP?name=$f"
  done
  echo "==> published: https://github.com/$REPO/releases/tag/$VERSION"
fi

echo; echo "==> $VERSION released locally: $N/$N images on $REG, CVE-clean, pinned, Release published."
