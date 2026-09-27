#!/usr/bin/env bash
# No curl|sh, no implicit sudo, no automatic service enablement.
# POSIX convenience wrapper. On Windows without bash run instead:
#   python scripts/install.py "$@"
set -Eeuo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
exec python3 "$ROOT/scripts/install.py" "$@"
