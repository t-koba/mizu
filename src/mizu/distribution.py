"""Shared public source inventory for packaging and candidate installation."""
import os
from pathlib import Path

from .fs import PRIVATE_EVIDENCE_TREES

DIRECTORIES = frozenset({'src', 'adapters', 'bin', 'docs', 'examples', 'scripts', 'tests', 'policies', 'config', 'containers', '.github'})
METADATA = frozenset({'README.md', 'Makefile', 'SECURITY.md', '.editorconfig', 'LICENSE', 'CHANGELOG.md', 'AGENTS.md', 'pyproject.toml', '.gitignore', 'DISTRIBUTION.json', 'MANIFEST.sha256'})
EXCLUDED = frozenset({'.git', 'node_modules', '__pycache__', '.pytest_cache', '.ruff_cache', '.venv', 'dist', 'build', '.DS_Store'}) | PRIVATE_EVIDENCE_TREES
PRIVATE = frozenset({'credentials.env', 'config.local.toml', 'installation.json', 'source-manifest.json', 'installation-checks.json'})

def is_private_name(name: str) -> bool:
    """Whether a base name is product-private and must never be captured or packaged.

    One owner for the "private names" rule: names the product treats as
    secrets or per-installation state, plus the ``.env`` family. Shared by
    the snapshot capture and the source packager so the two can never drift.
    """
    return name in PRIVATE or name.startswith(".env")

#: Versioned schema for the offline source-gate report reused by core-only
#: installation. Both scripts/check.py (producer) and scripts/install.py
#: (consumer) bind to this exact version; a mismatch is stale, not compatible.
OFFLINE_REPORT_VERSION = 1


def source_manifest(root: Path):
    """Shared distribution source manifest: contents plus executable bits."""
    from mizu.fs import digest
    return {relative.as_posix(): {"sha256": digest(path.read_bytes()), "executable": bool(path.stat().st_mode & 0o111)}
            for relative, path in source_files(root)}


def source_manifest_sha(root: Path) -> str:
    """Digest binding an offline report to its exact shared source tree."""
    from mizu.fs import canonical, digest
    return digest(canonical(source_manifest(root)))


def source_files(root: Path):
    files = []
    def add(path):
        if path.name in EXCLUDED or is_private_name(path.name) or path.suffix in ('.pyc', '.pyo'):
            return
        relative = path.relative_to(root)
        if path.is_symlink():
            raise ValueError('Refusing source symlink: ' + str(relative))
        if path.is_file():
            files.append((relative, path))
    for name in sorted(METADATA):
        add(root / name)
    if (root / 'config').is_symlink():
        raise ValueError('Refusing source symlink: config')
    add(root / 'config/config.example.toml')
    for name in sorted(DIRECTORIES - {'config'}):
        directory = root / name
        if directory.is_symlink():
            raise ValueError('Refusing source symlink: ' + name)
        if not directory.is_dir():
            continue
        for current, dirs, names in os.walk(directory, followlinks=False):
            retained = []
            for child in sorted(dirs):
                if child in EXCLUDED or is_private_name(child):
                    continue
                path = Path(current) / child
                if path.is_symlink():
                    raise ValueError('Refusing source symlink: ' + str(path.relative_to(root)))
                retained.append(child)
            dirs[:] = retained
            for child in sorted(names):
                add(Path(current) / child)
    yield from sorted(files)


#: Versioned identity of the public-source projection derived from a captured
#: snapshot. The projection is the deterministic subset of captured file
#: metadata that the curated distribution inventory would publish: the same
#: ``DIRECTORIES``/``METADATA`` scope, the same ``EXCLUDED`` pruning, and the
#: same ``is_private_name`` policy as :func:`source_files`. ``code_digest``
#: keeps its exact meaning -- the whole immutable capture -- and is never
#: recomputed from this subset; historical digests are never reinterpreted.
PUBLIC_SOURCE_SCHEMA = 1
PUBLIC_SOURCE_ALGORITHM = "mizu-public-source-v1"
#: Bound for a projection algorithm label carried in publication evidence.
PUBLIC_SOURCE_ALGORITHM_CHARS = 128


def is_public_source_path(relative: str) -> bool:
    """Whether a captured relative path belongs to the public projection.

    Schema: a ``/``-separated relative path with no empty/``.``/``..``
    components and no leading slash. Bounds: pure path syntax, no
    filesystem access. Trust: the curated inventory above, not operator
    excludes or Git ignore state. Failure: ``False`` for anything outside
    the published set (never raises).
    """
    if not isinstance(relative, str) or not relative or relative.startswith("/"):
        return False
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    for component in parts[:-1]:
        if component in EXCLUDED or is_private_name(component):
            return False
    name = parts[-1]
    if name in EXCLUDED or is_private_name(name):
        return False
    if Path(name).suffix in (".pyc", ".pyo"):
        return False
    if len(parts) == 1:
        return relative in METADATA
    top = parts[0]
    if top == "config":
        return relative == "config/config.example.toml"
    return top in DIRECTORIES


def public_projection(files: dict) -> dict:
    """Project captured file metadata onto the public-source manifest.

    Schema: ``files`` maps relative paths to ``{"sha256", "executable"}``
    entries (captured entries carry an extra ``bytes`` field, which is
    dropped); the result maps each public path to ``{"sha256",
    "executable"}`` exactly as :func:`source_manifest` shapes it, so the
    host adapter can recompute the same manifest from the Git publication
    tree and compare digests. Bounds: no filesystem access; input size is
    the caller's capture bound. Trust: caller-supplied capture metadata;
    malformed entries fail closed. Failure: ``ValueError``.
    """
    if not isinstance(files, dict):
        raise ValueError("Invalid captured files for public projection")
    from .fs import DIGEST as _DIGEST
    manifest: dict[str, dict] = {}
    for path, entry in files.items():
        if not isinstance(path, str) or not is_public_source_path(path):
            # Private names, excluded trees, and non-inventory paths never
            # enter the projection; malformed keys outside the inventory are
            # simply not public rather than an error, so a stranger file can
            # never move the projection digest by failing the derivation.
            continue
        if (not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str)
                or not _DIGEST.fullmatch(entry["sha256"])
                or type(entry.get("executable")) is not bool):
            raise ValueError(f"Invalid captured entry for public projection: {path}")
        manifest[path] = {"sha256": entry["sha256"], "executable": entry["executable"]}
    return manifest


def public_source_digest(files: dict) -> str:
    """Digest of the public-source projection of captured file metadata."""
    from mizu.fs import canonical, digest
    return digest(canonical(public_projection(files)))


def public_source_identity(files: dict) -> dict:
    """Versioned identity of the public-source projection.

    Schema: ``{"schema", "algorithm", "digest"}`` with the constants
    above. The digest binds the projection manifest, not the whole capture;
    it must never be stored or compared as a ``code_digest``.
    """
    return {"schema": PUBLIC_SOURCE_SCHEMA, "algorithm": PUBLIC_SOURCE_ALGORITHM,
            "digest": public_source_digest(files)}
