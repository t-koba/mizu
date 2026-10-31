#!/usr/bin/env bash
# Separate from unittest: the installer itself runs unittest as its staging gate.
set -euo pipefail
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
TEMP=$(mktemp -d)
trap 'rm -rf -- "$TEMP"' EXIT
PREFIX="$TEMP/prefix with spaces"
BIN="$TEMP/bin with spaces"
CONFIG="$TEMP/private configuration/config.toml"
# No arguments: full staged source gate. A report is the separate existing
# core-only local reuse mode; CI selection reads only trusted retained proof.
REUSE_ARGS=()
EXPECTED_PROVENANCE="staged-gate"
usage() { echo "usage: test-install.sh [source-report.json | --reuse-ci-checks CONFIG PROJECT SNAPSHOT]"; }
if [ "$#" -eq 4 ] && [ "$1" = "--reuse-ci-checks" ]; then
  REUSE_ARGS=("$@")
  EXPECTED_PROVENANCE="retained-ci-acceptance"
elif [ "$#" -eq 1 ]; then
  case "$1" in
    -h|--help) usage; exit 0 ;;
    -*) usage >&2; exit 2 ;;
  esac
  REUSE_ARGS=(--reuse-source-checks "$1")
  EXPECTED_PROVENANCE="reused-source-gate"
elif [ "$#" -ne 0 ]; then
  usage >&2; exit 2
fi
"$ROOT/scripts/setup.sh" --core-only --prefix "$PREFIX" --bin-dir "$BIN" --config "$CONFIG" "${REUSE_ARGS[@]}" > "$TEMP/install.json"
"$BIN/mizu" --version
python3 - "$CONFIG" "$TEMP" <<'PY'
import json, pathlib, sys
config, root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
config.write_text(config.read_text().replace('data_dir = "~/.local/state/mizu"', 'data_dir = ' + json.dumps(str(root / 'state'))))
(root / 'configuration-before').write_bytes(config.read_bytes())
PY
"$ROOT/scripts/setup.sh" --core-only --prefix "$PREFIX" --bin-dir "$BIN" --config "$CONFIG" "${REUSE_ARGS[@]}" > "$TEMP/reinstall.json"
cmp -- "$CONFIG" "$TEMP/configuration-before"
"$BIN/mizu" --config "$CONFIG" init demo --source "$ROOT/examples/demo" --goal "$ROOT/examples/PROJECT.md" --roles worker --verify 'python3 -m unittest discover -v' > "$TEMP/init.json"
"$BIN/mizu" --config "$CONFIG" report demo > "$TEMP/report.json"
"$BIN/mizu" --config "$CONFIG" editor export demo "$TEMP/editor bundle" > "$TEMP/editor.json"
"$BIN/mizu" --config "$CONFIG" backup demo "$TEMP/private-backup.tar.gz" > "$TEMP/backup.json"
"$BIN/mizu" --config "$CONFIG" restore restored --archive "$TEMP/private-backup.tar.gz" > "$TEMP/restore.json"
"$BIN/mizu" --config "$CONFIG" status restored > "$TEMP/status.json"
python3 - "$TEMP" "$PREFIX" "$EXPECTED_PROVENANCE" <<'PY'
import json, pathlib, sys
root, prefix, expected_provenance = sys.argv[1], pathlib.Path(sys.argv[2]), sys.argv[3]
root = pathlib.Path(root)
assert json.loads((root / 'restore.json').read_text())['armed'] is False
assert json.loads((root / 'status.json').read_text())['control']['paused'] is True
receipt = json.loads((prefix / 'current/installation.json').read_text())
assert receipt['validation'] == 'local-install-checks-passed'
assert receipt['pi_version'] is None
assert receipt.get('checks_provenance') == expected_provenance, receipt.get('checks_provenance')
print('PASS: offline installation, spaces, idempotence, source integrity, CLI import/report/editor/backup/restore')
PY
