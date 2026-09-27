#!/usr/bin/env bash
# Run the Editor capsule in a Linux container via any OCI runtime.
# Override the runtime with MIZU_RUNTIME=docker (Podman and Docker Desktop both work).
# The image must contain the chosen Editor CLI and Python >=3.11.
set -Eeuo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
EXE="${MIZU_RUNTIME:-podman}"
IMAGE= BUNDLE= OUTBOX= ENV_FILE=
while (($#)); do
  case "$1" in
    --image) IMAGE=${2:?}; shift 2;;
    --bundle) BUNDLE=${2:?}; shift 2;;
    --outbox) OUTBOX=${2:?}; shift 2;;
    --env-file) ENV_FILE=${2:?}; shift 2;;
    --) shift; break;;
    *) echo 'Usage: editor-capsule.sh --image DIGEST --bundle DIR --outbox DIR [--env-file FILE] -- EDITOR_COMMAND [ARGS...]' >&2; exit 64;;
  esac
done
[[ $(id -u) != 0 && -n $IMAGE && -d $BUNDLE && -n $OUTBOX && $# -gt 0 ]] || { echo 'Missing arguments or privileged user.' >&2; exit 64; }
[[ $IMAGE =~ (^sha256:[0-9a-f]{64}$|@sha256:[0-9a-f]{64}$) ]] || { echo 'Pin the Editor image by digest.' >&2; exit 78; }
[[ -f $BUNDLE/snapshot.json && -f $OUTBOX/.mizu-outbox ]] || { echo 'Use a Mizu export and the dedicated project spool/editor outbox.' >&2; exit 78; }
PYTHONPATH="$ROOT/src" python3 - "$BUNDLE" "$OUTBOX" <<'PYCHECK'
from pathlib import Path
import sys
from mizu.editor import Capsule
Capsule(Path(sys.argv[1]), Path(sys.argv[2]))
PYCHECK
BUNDLE=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$BUNDLE")
OUTBOX=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$OUTBOX")
UID_GID="$(id -u):$(id -g)"
if [[ $(uname -s) == Linux ]]; then
  # `-v` splits on `:` (safe here: no drive letters) and keeps `:z` relabeling.
  for path in "$ROOT" "$BUNDLE" "$OUTBOX"; do [[ $path != *:* && $path != *$'\n'* && $path != *,* ]] || exit 78; done
  MOUNTS=(--volume "$ROOT:/opt/mizu:ro,z" --volume "$BUNDLE:/bundle:ro,z"
          --volume "$OUTBOX:/outbox:rw,z")
else
  # `--mount` splits on `,` only, so Windows drive letters survive.
  for path in "$ROOT" "$BUNDLE" "$OUTBOX"; do [[ $path != *$'\n'* && $path != *,* ]] || exit 78; done
  MOUNTS=(--mount "type=bind,src=$ROOT,dst=/opt/mizu,readonly"
          --mount "type=bind,src=$BUNDLE,dst=/bundle,readonly"
          --mount "type=bind,src=$OUTBOX,dst=/outbox")
fi
EXTRA=()
if [[ -n $ENV_FILE ]]; then
  python3 - "$ENV_FILE" <<'PY'
import os, stat, sys
p=sys.argv[1]; s=os.stat(p)
uid = getattr(os, "getuid", None)
owner_ok = True if uid is None else s.st_uid == uid
assert not os.path.islink(p) and owner_ok and not s.st_mode & 0o077, 'Private, owned environment file required'
PY
  EXTRA+=(--env-file "$ENV_FILE")
fi
# Editor inference needs external networking. Unlike execution sandboxes, this
# capsule has explicitly documented outbound network access and a narrow mount set.
exec "$EXE" run --rm --interactive --tty --pull=never \
  --user "$UID_GID" --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  --memory=2g --pids-limit=256 --cpus=2 \
  --tmpfs /tmp:rw,nosuid,nodev,size=512m,mode=1777 --env HOME=/tmp \
  "${MOUNTS[@]}" --workdir /bundle/code "${EXTRA[@]}" \
  --entrypoint /bin/sh "$IMAGE" -c 'exec "$@"' sh "$@"
