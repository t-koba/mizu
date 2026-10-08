#!/usr/bin/env bash
# Separate from unittest: the installer itself runs unittest as its staging gate.
set -euo pipefail
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
TEMP=$(mktemp -d)
trap 'rm -rf -- "$TEMP"' EXIT
PREFIX="$TEMP/prefix with spaces"
BIN="$TEMP/bin with spaces"
CONFIG="$TEMP/private configuration/config.toml"
"$ROOT/scripts/setup.sh" --core-only --prefix "$PREFIX" --bin-dir "$BIN" --config "$CONFIG" > "$TEMP/install.json"
"$BIN/mizu" --version
python3 - "$CONFIG" "$TEMP" <<'PY'
import json, pathlib, sys
config, root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
config.write_text(config.read_text().replace('data_dir = "~/.local/state/mizu"', 'data_dir = ' + json.dumps(str(root / 'state'))))
(root / 'configuration-before').write_bytes(config.read_bytes())
PY
"$ROOT/scripts/setup.sh" --core-only --prefix "$PREFIX" --bin-dir "$BIN" --config "$CONFIG" > "$TEMP/reinstall.json"
cmp -- "$CONFIG" "$TEMP/configuration-before"
"$BIN/mizu" --config "$CONFIG" init demo --source "$ROOT/examples/demo" --goal "$ROOT/examples/PROJECT.md" --roles worker --verify 'python3 -m unittest discover -v' > "$TEMP/init.json"
"$BIN/mizu" --config "$CONFIG" report demo > "$TEMP/report.json"
"$BIN/mizu" --config "$CONFIG" editor export demo "$TEMP/editor bundle" > "$TEMP/editor.json"
"$BIN/mizu" --config "$CONFIG" backup demo "$TEMP/private-backup.tar.gz" > "$TEMP/backup.json"
"$BIN/mizu" --config "$CONFIG" restore restored --archive "$TEMP/private-backup.tar.gz" > "$TEMP/restore.json"
"$BIN/mizu" --config "$CONFIG" status restored > "$TEMP/status.json"
python3 - "$TEMP" "$PREFIX" <<'PY'
import json, pathlib, sys
root, prefix = map(pathlib.Path, sys.argv[1:])
assert json.loads((root / 'restore.json').read_text())['armed'] is False
assert json.loads((root / 'status.json').read_text())['control']['paused'] is True
receipt = json.loads((prefix / 'current/installation.json').read_text())
assert receipt['validation'] == 'local-install-checks-passed'
assert receipt['pi_version'] is None
print('PASS: offline installation, spaces, idempotence, source integrity, CLI import/report/editor/backup/restore')
PY
