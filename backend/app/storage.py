"""Opaque-key file storage on a local directory.

Files live under STORAGE_PATH in namespaces ("assets", "sources", later "imports"), named by a
random 32-hex KEY that the database row stores. A user-supplied name is NEVER part of a path: keys
are generated here and validated against a strict pattern before any path is built, so no input
can traverse out of the directory.

Writes go to a temporary file inside STORAGE_PATH/tmp first and are moved into place with
os.replace (atomic on one filesystem), so a reader never sees a half-written file. A crash between
the move and the database insert leaves an orphan file; `python -m app.commands.cleanup_documents`
removes files that have no row after 24 hours.

DEPLOYMENT: the API and the workers must see the SAME directory (a shared volume in Docker Compose).
A multi-host deployment needs a shared mount (NFS, a clustered filesystem) or a different adapter
behind this same small interface; no paid object storage is assumed.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import tempfile
from collections.abc import Iterator
from pathlib import Path

_KEY = re.compile(r"[0-9a-f]{32}")
NAMESPACES = ("assets", "imports", "sources")


class StorageError(Exception):
    pass


class Storage:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).resolve()

    # ---- layout ----

    @property
    def tmp_dir(self) -> Path:
        path = self.root / "tmp"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def new_key(self) -> str:
        return secrets.token_hex(16)

    def path_for(self, namespace: str, key: str) -> Path:
        if namespace not in NAMESPACES or not _KEY.fullmatch(key):
            raise StorageError("invalid storage key")
        return self.root / namespace / key[:2] / key[2:4] / key

    def temp_file(self) -> tuple[int, Path]:
        """A new empty temporary file (an open OS handle and its path) inside the storage root."""
        fd, name = tempfile.mkstemp(dir=self.tmp_dir, prefix="up-")
        return fd, Path(name)

    # ---- operations ----

    def put(self, namespace: str, key: str, source: Path) -> None:
        """Atomically move `source` (a file inside tmp/) to its final place."""
        dest = self.path_for(namespace, key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, dest)

    def exists(self, namespace: str, key: str) -> bool:
        return self.path_for(namespace, key).is_file()

    def delete(self, namespace: str, key: str) -> bool:
        """Remove a file; True if it existed. Idempotent."""
        try:
            self.path_for(namespace, key).unlink()
            return True
        except FileNotFoundError:
            return False

    def iter_files(self, namespace: str) -> Iterator[tuple[str, Path, float]]:
        """(key, path, mtime) for every stored file in a namespace."""
        base = self.root / namespace
        if not base.is_dir():
            return
        for path in base.rglob("*"):
            if path.is_file() and _KEY.fullmatch(path.name):
                with contextlib.suppress(FileNotFoundError):
                    yield path.name, path, path.stat().st_mtime

    def stale_temp_files(self, older_than_seconds: float, now: float) -> Iterator[Path]:
        base = self.root / "tmp"
        if not base.is_dir():
            return
        for path in base.iterdir():
            with contextlib.suppress(FileNotFoundError):
                if path.is_file() and now - path.stat().st_mtime > older_than_seconds:
                    yield path
