#!/usr/bin/env bash
# The only intentionally privileged script. Linux production only.
# macOS/Windows development does not use it; install Python/Git/Node manually.
set -Eeuo pipefail
[[ $(uname -s) == Linux ]] || { echo 'system-deps.sh is Linux-only; on macOS/Windows install Python >=3.11, Git and Node >=22.19.0 manually.' >&2; exit 78; }
[[ ${1:-} == --install ]] || { echo 'Usage: sudo scripts/system-deps.sh --install' >&2; exit 64; }
[[ $(id -u) == 0 ]] || { echo 'Run this one script as root; run Mizu itself as a normal user.' >&2; exit 77; }
if command -v apt-get >/dev/null; then
  apt-get update
  apt-get install -y python3 git podman uidmap slirp4netns fuse-overlayfs ca-certificates curl xz-utils
elif command -v dnf >/dev/null; then
  dnf install -y python3 git podman shadow-utils slirp4netns fuse-overlayfs ca-certificates curl xz
else
  echo 'Unsupported package manager. Install Python >=3.11, Git, rootless Podman, subordinate UID/GID tools, curl and xz.' >&2
  exit 78
fi
printf '%s\n' 'Also install Node >=22.19.0 plus npm. Configure subordinate UIDs/GIDs and cgroup v2 for the unprivileged service account.'
