# Releasing

Proprietary software. **All rights reserved.** See `LICENSE` — no licence is granted to
anyone. Keep the repository **private**; a proprietary licence on a public repo means the
world can read, clone and copy the source, and the licence only tells them they may not.

---

## One-time setup

1. **The licence is deliberately nameless.** Copyright exists from the moment the code is
   written; it is not conditional on a notice, on registration, or on naming the holder. The
   Owner is evidenced by the git history and the hosting account.

   Name a holder on the copyright line as soon as one exists — you personally, or a company.
   A named holder is who a licensee pays and who a court can identify. Until then the right is
   yours, but harder to assert.

2. **Create a PRIVATE repository** on GitHub. Do not use "public" and rely on the licence.

   ```bash
   gh repo create <owner>/base-services --private --source . --remote origin
   # or, without gh:
   git remote add origin git@github.com:<owner>/base-services.git
   ```

3. **Push.**

   ```bash
   git push -u origin main
   ```

4. **Check the package visibility once, after the first release.** GHCR packages inherit the
   repo's visibility at creation, but the setting lives on the *package*, not the repo. Open
   `github.com/users/<owner>/packages` and confirm every `base-services/*` image says
   **Private**. A public image ships your source in its layers.

---

## Cutting a release

A release is not "some images were pushed". It is **these exact digests, and the chart renders
with them**. `.github/workflows/release.yml` enforces that, in this order:

1. `run_all_gates.sh --strict` — the same gates that block a merge. A release never ships code
   CI would have rejected. A skipped gate is a failed gate.
2. Build and push one image per service to GHCR, **by digest**, with a signed
   build-provenance attestation, then scan it with Trivy. A `HIGH`/`CRITICAL` finding fails
   the release.
3. Rewrite `deploy/services/*.values.yaml` with the published digests, verify **nothing** is
   left unpinned, and prove the chart renders against every one of them.
4. Publish a GitHub Release with the pinned values, the digest map, and checksums.

```bash
git tag -a v0.1.0 -m "First release"
git push origin v0.1.0
```

Or run it by hand: **Actions → release → Run workflow → `v0.1.0`**.

## Why digests, not tags

The chart refuses an unpinned image. A `:latest` tag with `imagePullPolicy: IfNotPresent`
silently redeploys whatever is already on the node — the rollout is green and the code is old.
`tools/release/pin_digests.py --verify` is the gate; it validates every digest **before**
writing anything, because a half-pinned release directory looks deployable.

You can run the pinning locally against any digest map:

```bash
python tools/release/pin_digests.py --digests digests.json \
    --registry ghcr.io/<owner> --out deploy/pinned
python tools/release/pin_digests.py --verify deploy/pinned
```

## Deploying a release

```bash
tar xzf deploy-pinned-v0.1.0.tar.gz
cd deploy && make verify              # proves the cluster enforces what the chart promises
helm upgrade --install wallet-service ./chart -f pinned/wallet-service.values.yaml
```

`make deploy` depends on `make verify`: we do not install onto a cluster whose deny-by-default
NetworkPolicy is decorative, or whose HPAs can never scale.

## Verifying an image really came from this repo

Every digest carries a signed provenance attestation, so a digest is not just a number someone
typed:

```bash
gh attestation verify \
  oci://ghcr.io/<owner>/wallet-service@sha256:… \
  --repo <owner>/base-services
```

## Pulling a private image in a cluster

The images are private, so the cluster needs a pull secret. Reference it from the service's
values file (`image.pullSecrets`), never from the chart's defaults:

```bash
kubectl create secret docker-registry ghcr \
  --docker-server=ghcr.io --docker-username=<owner> \
  --docker-password=<a PAT with read:packages> \
  -n base-services
```

## Before every push

`local == CI`. Run the gates yourself:

```bash
tools/ci/run_all_gates.sh --profile quick    # seconds
tools/ci/run_all_gates.sh                    # everything runnable here
tools/ci/run_all_gates.sh --list             # what each gate protects
```

The `safety:secrets` gate refuses a diff that introduces a private key, a `.env`, or a
`.secrets` file. It cannot un-leak one: a credential committed once is a credential rotated,
not a commit reverted.
