#!/usr/bin/env bash
# Build the Linux sandbox image with any OCI runtime (Podman or Docker Desktop).
# Override the runtime with MIZU_RUNTIME=docker.
set -Eeuo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
EXE="${MIZU_RUNTIME:-podman}"
BASE= TAG=localhost/mizu-sandbox:local APT_SANDBOX_USER=
while (($#)); do
  case "$1" in
    --base) BASE=${2:?}; shift 2;;
    --tag) TAG=${2:?}; shift 2;;
    --apt-sandbox-user) APT_SANDBOX_USER=${2:?}; shift 2;;
    *) echo 'Usage: build-sandbox.sh --base DEBIAN_BASED_PYTHON_IMAGE [--tag LOCAL_TAG] [--apt-sandbox-user USER]' >&2; exit 64;;
  esac
done
[[ $(id -u) != 0 && -n $BASE ]] || { echo 'Use a normal user and explicitly select a Python >=3.11 Debian-based base image.' >&2; exit 77; }
command -v "$EXE" >/dev/null || { echo "Container runtime not found: $EXE (set MIZU_RUNTIME)." >&2; exit 78; }
"$EXE" pull -- "$BASE" >&2
# A Podman checkpoint image restores its own config under `run` and silently
# ignores sandbox flags on unpatched Podman (CVE-2026-94603). Refuse it here
# so neither the base nor the built tag can carry the marker forward.
CHECKPOINT_ANNOTATION='io.podman.annotations.checkpoint.runtime.name'
refuse_checkpoint() {
  local ref="$1"
  if "$EXE" image inspect -- "$ref" 2>/dev/null | grep -qF "$CHECKPOINT_ANNOTATION"; then
    echo "Refusing checkpoint image: $ref carries $CHECKPOINT_ANNOTATION (CVE-2026-94603)." >&2
    echo 'Rebuild from a clean base image.' >&2
    exit 76
  fi
}
refuse_checkpoint "$BASE"
BASE_ID=$("$EXE" image inspect --format '{{.Id}}' "$BASE")
"$EXE" build --pull=never --build-arg "BASE_IMAGE=$BASE_ID" --build-arg "APT_SANDBOX_USER=$APT_SANDBOX_USER" --tag "$TAG" --file "$ROOT/containers/Containerfile" "$ROOT/containers" >&2
refuse_checkpoint "$TAG"
IMAGE=$("$EXE" image inspect --format '{{.Id}}' "$TAG")
[[ $IMAGE =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Unexpected image ID' >&2; exit 76; }
printf 'Base resolved to %s\nSet sandbox.image to the image ID printed on stdout. Archive build logs for provenance.\n' "$BASE_ID" >&2
printf '%s\n' "$IMAGE"
