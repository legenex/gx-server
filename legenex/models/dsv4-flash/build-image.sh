#!/usr/bin/env bash
# build-image.sh — build the dsv4-flash runtime image from pinned, hash-checked inputs.
#
#   ./build-image.sh            fetch/verify vendor files, pull base by digest, build
#   ./build-image.sh --verify   only fetch + verify the vendor cache (no docker)
#
# Runs as the normal user (docker group, no sudo). Security review conditions:
#   1. FROM vllm/vllm-openai@sha256:2cc49b81... (arm64 v0.25.1), never anemll images;
#   2. b12x + FlashInfer tarballs and the PyPI wheels are verified against
#      vendor-files.txt BEFORE they enter the build context; pip runs with
#      --no-index (wheels --require-hashes) and the build itself has no network
#      (`docker build --network=none`), so nothing unpinned can be fetched;
#   3. BUILD_NVEP=0 and an NCCL before/after equality check (Dockerfile);
#   4. telemetry/offline env baked in.
# The resulting image id is printed and written to $GX_BUILD_RECORD (JSON).
set -euo pipefail

KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=pins.conf
source "$KIT/pins.conf"

VENDOR_CACHE="${GX_VENDOR_CACHE:-$HOME/.cache/gx-dsv4-flash/vendor}"
BUILD_RECORD="${GX_BUILD_RECORD:-$HOME/.cache/gx-dsv4-flash/image-build.json}"
IMAGE="${IMAGE_REPO}:${IMAGE_TAG}"

log() { printf '[dsv4-build] %s\n' "$*"; }
die() { printf '[dsv4-build] ERROR: %s\n' "$*" >&2; exit 1; }

umask 077
mkdir -p "$VENDOR_CACHE"
chmod 700 "$VENDOR_CACHE" "$(dirname "$VENDOR_CACHE")"

# ------------------------------------------------------------- vendor cache
fetch_and_verify() {
    local file sha url got
    while read -r file sha url; do
        case "$file" in ''|'#'*) continue ;; esac
        [[ "$file" =~ ^[A-Za-z0-9._+-]+$ ]] || die "unsafe vendor file name: $file"
        [[ "$sha" =~ ^[0-9a-f]{64}$ ]] || die "bad sha256 for $file"
        if [ -f "$VENDOR_CACHE/$file" ] && [ "$(sha256sum "$VENDOR_CACHE/$file" | cut -d' ' -f1)" = "$sha" ]; then
            log "cached  $file"
            continue
        fi
        log "fetch   $file"
        curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "$VENDOR_CACHE/$file.part" "$url"
        got="$(sha256sum "$VENDOR_CACHE/$file.part" | cut -d' ' -f1)"
        if [ "$got" != "$sha" ]; then
            rm -f "$VENDOR_CACHE/$file.part"
            die "sha256 mismatch for $file: got $got, pinned $sha"
        fi
        mv "$VENDOR_CACHE/$file.part" "$VENDOR_CACHE/$file"
    done < "$KIT/vendor-files.txt"
}

# Every wheel in requirements-pypi.txt must be a vendored file with the same hash.
cross_check_requirements() {
    local h
    for h in $(grep -o 'sha256:[0-9a-f]\{64\}' "$KIT/requirements-pypi.txt" | cut -d: -f2); do
        grep -q " $h " "$KIT/vendor-files.txt" || die "requirements-pypi.txt hash $h has no vendor-files.txt entry"
    done
    grep -q " ${B12X_SHA256} " "$KIT/vendor-files.txt" || die "pins.conf B12X_SHA256 not in vendor-files.txt"
    grep -q " ${FLASHINFER_SHA256} " "$KIT/vendor-files.txt" || die "pins.conf FLASHINFER_SHA256 not in vendor-files.txt"
}

cross_check_requirements
fetch_and_verify
log "vendor cache verified: $VENDOR_CACHE"
[ "${1:-}" = "--verify" ] && exit 0

command -v docker >/dev/null || die "docker not found"
[ "$(uname -m)" = "aarch64" ] || die "this image is arm64-only (uname -m = $(uname -m))"

# ------------------------------------------------------------- build context
CTX="$(mktemp -d "${TMPDIR:-/tmp}/gx-dsv4-ctx.XXXXXX")"
trap 'rm -rf "$CTX"' EXIT
mkdir -p "$CTX/vendor" "$CTX/tools"
cp "$KIT/Dockerfile.runtime" "$CTX/Dockerfile.runtime"
cp -a "$KIT/upstream-overlay" "$CTX/upstream-overlay"
cp -a "$KIT/gx_probe" "$CTX/gx_probe"
cp "$KIT/tools/nccl_fingerprint.py" "$KIT/tools/provenance.py" "$CTX/tools/"
cp "$KIT/requirements-pypi.txt" "$CTX/vendor/requirements-pypi.txt"
: > "$CTX/vendor/SHA256SUMS"
while read -r file sha _url; do
    case "$file" in ''|'#'*) continue ;; esac
    ln "$VENDOR_CACHE/$file" "$CTX/vendor/$file" 2>/dev/null || cp "$VENDOR_CACHE/$file" "$CTX/vendor/$file"
    printf '%s  %s\n' "$sha" "$file" >> "$CTX/vendor/SHA256SUMS"
done < "$KIT/vendor-files.txt"
# Overlay files must still match what was reviewed.
( cd "$CTX/upstream-overlay" && sha256sum -c --quiet OVERLAY.SHA256SUMS ) || die "overlay files differ from OVERLAY.SHA256SUMS"
chmod -R a+rX "$CTX"

# ------------------------------------------------------------- base image
if ! docker image inspect "$BASE_IMAGE" >/dev/null 2>&1; then
    log "pulling base $BASE_IMAGE"
    docker pull "$BASE_IMAGE"
fi
base_id="$(docker image inspect -f '{{.Id}}' "$BASE_IMAGE")"
log "base image id $base_id"

kit_commit="$(git -C "$KIT" rev-parse HEAD 2>/dev/null || echo "${GX_KIT_COMMIT:-unknown}")"
log "building $IMAGE (network disabled)"
DOCKER_BUILDKIT=1 docker build \
    --network=none \
    --pull=false \
    --file "$CTX/Dockerfile.runtime" \
    --build-arg "BASE_IMAGE=$BASE_IMAGE" \
    --build-arg "BASE_VLLM_SITE=$BASE_VLLM_SITE" \
    --build-arg "B12X_FILE=b12x-${B12X_COMMIT}.tar.gz" \
    --build-arg "FLASHINFER_FILE=flashinfer-${FLASHINFER_COMMIT}.tar.gz" \
    --build-arg "GX_KIT_COMMIT=$kit_commit" \
    --tag "$IMAGE" \
    "$CTX"

image_id="$(docker image inspect -f '{{.Id}}' "$IMAGE")"
provenance="$(docker run --rm --network none --entrypoint cat "$IMAGE" /opt/gx-build/provenance.json)"
nccl="$(docker run --rm --network none --entrypoint cat "$IMAGE" /opt/gx-build/nccl.after.json | python3 -I -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["distributions"]))')"
mkdir -p "$(dirname "$BUILD_RECORD")"
python3 -I - "$BUILD_RECORD" "$IMAGE" "$image_id" "$BASE_IMAGE" "$base_id" "$kit_commit" "$provenance" "$nccl" <<'PY'
import json, sys, time
path, image, image_id, base, base_id, commit, prov, nccl = sys.argv[1:9]
json.dump({"image": image, "image_id": image_id, "base_image": base, "base_image_id": base_id,
           "kit_commit": commit, "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "provenance": json.loads(prov), "nccl": json.loads(nccl)}, open(path, "w"), indent=1)
PY
log "built $IMAGE"
log "IMAGE_ID=$image_id"
log "record: $BUILD_RECORD"
