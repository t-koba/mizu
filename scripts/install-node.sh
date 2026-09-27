#!/usr/bin/env bash
# An exact, caller-verified Node archive; no executable network bootstrap.
# Linux production helper. On macOS/Windows install Node >=22.19.0 manually
# from nodejs.org and verify the published checksum/signature (see docs/upstream.md).
set -Eeuo pipefail
[[ $(uname -s) == Linux ]] || { echo 'install-node.sh is Linux-only; on macOS/Windows install Node manually.' >&2; exit 78; }
VERSION= SHA= PREFIX="${HOME}/.local/share/mizu-tools/node"
while (($#)); do
  case "$1" in
    --version) VERSION=${2:?}; shift 2;;
    --sha256) SHA=${2:?}; shift 2;;
    --prefix) PREFIX=${2:?}; shift 2;;
    *) echo 'Usage: install-node.sh --version X.Y.Z --sha256 VERIFIED_HASH [--prefix DIRECTORY]' >&2; exit 64;;
  esac
done
[[ $(id -u) != 0 ]] || { echo 'Install Node as an unprivileged user.' >&2; exit 77; }
[[ $VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ && $SHA =~ ^[0-9a-f]{64}$ ]] || { echo 'An exact version and SHA-256 are required.' >&2; exit 64; }
case $(uname -m) in x86_64) ARCH=x64;; aarch64) ARCH=arm64;; *) echo 'Supported architectures: x86_64, aarch64' >&2; exit 78;; esac
NAME="node-v${VERSION}-linux-${ARCH}"
DEST="$PREFIX/$VERSION"
[[ ! -e $DEST ]] || { echo "Already exists: $DEST" >&2; exit 77; }
mkdir -p -- "$PREFIX"
TMP=$(mktemp -d "$PREFIX/.node-XXXXXXXX")
trap 'rm -rf -- "$TMP"' EXIT
curl --fail --show-error --silent --location --proto '=https' --tlsv1.2 \
  "https://nodejs.org/dist/v${VERSION}/${NAME}.tar.xz" -o "$TMP/node.tar.xz"
printf '%s  %s\n' "$SHA" "$TMP/node.tar.xz" | sha256sum --check --status
tar --extract --xz --file "$TMP/node.tar.xz" --directory "$TMP" --no-same-owner --no-same-permissions
"$TMP/$NAME/bin/node" -e 'const v=process.versions.node.split(".").map(Number); if(v[0]<22 || (v[0]===22 && v[1]<19))process.exit(78)'
mv -- "$TMP/$NAME" "$DEST"
printf 'Installed: %s\nAdd this directory to PATH before setup: %s/bin\n' "$DEST" "$DEST"
