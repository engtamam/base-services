#!/usr/bin/env bash
# =============================================================================
# release.sh — push, WAIT for CI to go green on this exact commit, then tag.
#
# The release ALWAYS runs its own gates — we do not skip them. (We tried: trusting a
# green CI for the same commit saved ~9 minutes, then GitHub propagated the resulting
# `skipped` transitively and v0.2.0 published NOTHING while reporting success. A
# release that can silently ship nothing is not worth nine minutes.)
#
# This script is still the right way to cut one: it refuses to tag a commit CI has
# not proven, and refuses outright to tag one CI has FAILED. Tagging a red commit
# just to watch the release fail again wastes far more than it saves.
#
#   scripts/release.sh v0.1.6
#   scripts/release.sh v0.1.6 --dry-run     # do everything except create/push the tag
#
# Auth: uses gh if present, else $GH_TOKEN / $GITHUB_TOKEN, else the token git
# already stored for github.com. Read-only API calls; the token is never printed.
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="${1:-}"
DRY_RUN=0
[ "${2:-}" = "--dry-run" ] && DRY_RUN=1

die() { echo "ERROR: $*" >&2; exit 1; }

[ -n "$VERSION" ] || die "usage: scripts/release.sh vX.Y.Z [--dry-run]"
echo "$VERSION" | grep -qE '^v[0-9]+\.[0-9]+\.[0-9]+$' \
  || die "version must look like v1.2.3, got: $VERSION"

# ── preconditions ───────────────────────────────────────────────────────────
[ -z "$(git status --porcelain)" ] || die "working tree is dirty — commit or stash first"
git rev-parse -q --verify "refs/tags/$VERSION" >/dev/null \
  && die "tag $VERSION already exists"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
SHA="$(git rev-parse HEAD)"
SHORT="$(git rev-parse --short HEAD)"

REMOTE_URL="$(git remote get-url origin)"
# git@github.com:owner/repo.git  |  https://github.com/owner/repo.git
REPO="$(echo "$REMOTE_URL" | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')"
[ -n "$REPO" ] || die "could not parse owner/repo from $REMOTE_URL"

# ── token (never printed) ───────────────────────────────────────────────────
TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
if [ -z "$TOKEN" ] && ! command -v gh >/dev/null 2>&1; then
  TOKEN="$(printf 'protocol=https\nhost=github.com\n\n' | git credential fill 2>/dev/null \
           | sed -n 's/^password=//p')"
fi

api() {  # api <path>  -> JSON on stdout
  if command -v gh >/dev/null 2>&1; then
    gh api "$1" 2>/dev/null
  else
    [ -n "$TOKEN" ] || die "no GitHub token (set GH_TOKEN, or log in with gh)"
    curl -sS -H "Authorization: Bearer $TOKEN" -H "Accept: application/vnd.github+json" \
      "https://api.github.com/$1"
  fi
}

# The conclusion of the newest `ci` run for a commit: success | failure | <pending> | none.
ci_result() {
  api "repos/$REPO/actions/runs?head_sha=$1&per_page=20" | python -c "
import json,sys
try: runs=json.load(sys.stdin).get('workflow_runs',[])
except Exception: print('none'); sys.exit()
ci=[r for r in runs if r.get('name')=='ci']
if not ci: print('none'); sys.exit()
r=ci[0]
print(r['conclusion'] if r['status']=='completed' else 'pending')
"
}

# ── push ────────────────────────────────────────────────────────────────────
echo "==> $VERSION  <-  $BRANCH @ $SHORT   ($REPO)"
if [ -n "$(git log "origin/$BRANCH..HEAD" --oneline 2>/dev/null)" ] \
   || ! git rev-parse -q --verify "origin/$BRANCH" >/dev/null; then
  echo "==> pushing $BRANCH"
  git push origin "$BRANCH" || die "push failed"
else
  echo "==> $BRANCH already up to date on origin"
fi

# ── wait for green ──────────────────────────────────────────────────────────
echo "==> waiting for a green ci run on $SHORT (CI must PROVE this commit before we tag)"
DEADLINE=$(( $(date +%s) + 1800 ))     # 30 minutes
while :; do
  RESULT="$(ci_result "$SHA")"
  case "$RESULT" in
    success) echo "    ci: SUCCESS — safe to tag"; break ;;
    failure|cancelled|timed_out|startup_failure)
      die "ci concluded '$RESULT' for $SHORT — refusing to tag a red commit" ;;
    pending) echo "    ci: running…" ;;
    none)    echo "    ci: no run yet…" ;;
    *)       echo "    ci: $RESULT" ;;
  esac
  [ "$(date +%s)" -lt "$DEADLINE" ] || die "timed out waiting for ci on $SHORT"
  sleep 20
done

# ── tag ─────────────────────────────────────────────────────────────────────
MSG="$(printf '%s\n\nReleased from %s @ %s after ci passed for this commit.' "$VERSION" "$BRANCH" "$SHORT")"
if [ "$DRY_RUN" = "1" ]; then
  echo "==> DRY RUN — would create and push tag $VERSION with message:"
  printf '%s\n' "$MSG" | sed 's/^/      /'
  exit 0
fi
echo "==> tagging $VERSION"
git tag -a "$VERSION" -m "$MSG" || die "tag failed"
git push origin "$VERSION" || die "tag push failed"
echo "==> $VERSION pushed. The release will run the full gates and publish."
