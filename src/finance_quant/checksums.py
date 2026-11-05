"""Shared deterministic SHA-256 primitives used by storage and provenance code."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

_CHUNK_SIZE = 1024 * 1024


def sha256_bytes(value: bytes) -> str:
    """Return the SHA-256 digest for an in-memory byte sequence."""
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    """Return the SHA-256 digest for UTF-8 text."""
    return sha256_bytes(value.encode("utf-8"))


def sha256_parts(parts: Iterable[bytes]) -> str:
    """Hash ordered byte sequences without first joining them in memory."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part)
    return digest.hexdigest()


def file_sha256(path: str | Path) -> str:
    """Hash one file without loading the complete payload into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def files_sha256(paths: Iterable[str | Path]) -> str:
    """Hash ordered file contents into one provenance digest."""
    digest = hashlib.sha256()
    for path in paths:
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(_CHUNK_SIZE), b""):
                digest.update(chunk)
    return digest.hexdigest()


def canonical_json_sha256(document: Mapping[str, Any] | list[Any]) -> str:
    """Hash JSON using the project's deterministic serialization policy."""
    encoded = json.dumps(document, sort_keys=True, allow_nan=False, separators=(",", ":"))
    return sha256_text(encoded)
