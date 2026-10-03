"""Shared public source inventory for packaging and candidate installation."""
import os
from pathlib import Path

DIRECTORIES = frozenset({'src', 'adapters', 'bin', 'docs', 'examples', 'scripts', 'tests', 'policies', 'config', 'containers', '.github'})
METADATA = frozenset({'README.md', 'Makefile', 'SECURITY.md', '.editorconfig', 'LICENSE', 'CHANGELOG.md', 'AGENTS.md', 'pyproject.toml', '.gitignore', 'DISTRIBUTION.json', 'MANIFEST.sha256'})
EXCLUDED = frozenset({'.git', 'node_modules', '__pycache__', '.pytest_cache', '.ruff_cache', '.venv', 'dist', 'build', 'private-validation', '.DS_Store'})
PRIVATE = frozenset({'credentials.env', 'config.local.toml', 'installation.json', 'source-manifest.json', 'installation-checks.json'})


def source_files(root: Path):
    files = []
    def add(path):
        if path.name in EXCLUDED | PRIVATE or path.name.startswith('.env') or path.suffix in ('.pyc', '.pyo'):
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
                if child in EXCLUDED | PRIVATE or child.startswith('.env'):
                    continue
                path = Path(current) / child
                if path.is_symlink():
                    raise ValueError('Refusing source symlink: ' + str(path.relative_to(root)))
                retained.append(child)
            dirs[:] = retained
            for child in sorted(names):
                add(Path(current) / child)
    yield from sorted(files)
