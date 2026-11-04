#!/usr/bin/env bash
# Run on a network-enabled workstation, review the result, then commit both locks.
set -Eeuo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
for adapter in pi pi-durable; do
  cd "$ROOT/adapters/$adapter"
  npm install --package-lock-only --ignore-scripts --engine-strict --no-audit --no-fund
done
printf '%s\n' 'Review both package-lock.json files and commit them before publishing a dependency-locked release.'
