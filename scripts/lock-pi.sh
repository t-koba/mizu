#!/usr/bin/env bash
# Run on a network-enabled workstation, review the result, then commit the lock.
set -Eeuo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT/adapters/pi"
npm install --package-lock-only --ignore-scripts --engine-strict --no-audit --no-fund
printf '%s\n' 'Review package-lock.json and commit it before publishing a dependency-locked release.'
