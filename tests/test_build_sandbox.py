import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build-sandbox.sh"


@unittest.skipUnless(os.name != "nt" and shutil.which("bash"), "requires a POSIX shell")
class BuildSandboxScriptTests(unittest.TestCase):
    def run_script(self, image_id):
        with tempfile.TemporaryDirectory() as temporary:
            bin_dir = Path(temporary) / "bin"
            bin_dir.mkdir()
            fake_id = bin_dir / "id"
            fake_id.write_text("#!/bin/sh\nprintf '1000\\n'\n")
            fake_runtime = bin_dir / "podman"
            fake_runtime.write_text("""#!/bin/sh
set -eu
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
    if [ "$3" = "--format" ]; then
        case "$5" in
            fake-base) printf '%s\n' "$FAKE_BASE_ID" ;;
            *) printf '%s\n' "$FAKE_IMAGE_ID" ;;
        esac
    elif [ "$3" = "--" ]; then
        printf '%s\n' '{"Config":{"Env":["PATH=/usr/local/bin"]}}'
    fi
    exit 0
fi
if [ "$1" = "pull" ] || [ "$1" = "build" ]; then
    exit 0
fi
echo "unexpected fake runtime arguments: $*" >&2
exit 99
""")
            fake_id.chmod(0o755)
            fake_runtime.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
            env["MIZU_RUNTIME"] = "podman"
            env["FAKE_BASE_ID"] = "b" * 64
            env["FAKE_IMAGE_ID"] = image_id
            return subprocess.run(
                [shutil.which("bash"), str(SCRIPT), "--base", "fake-base"],
                capture_output=True, text=True, env=env, timeout=10,
            )

    def test_normalizes_bare_podman_image_id(self):
        image_id = "a" * 64
        result = self.run_script(image_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"sha256:{image_id}\n")

    def test_keeps_sha256_prefixed_image_id(self):
        image_id = "c" * 64
        result = self.run_script(f"sha256:{image_id}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"sha256:{image_id}\n")

    def test_rejects_malformed_image_id(self):
        result = self.run_script("not-an-image-id")
        self.assertEqual(result.returncode, 76)
        self.assertIn("Unexpected image ID", result.stderr)


if __name__ == "__main__":
    unittest.main()
