"""Code provenance for cached research artifacts.

Every artifact fingerprint covers the complete computation layer, so a change in any module that
can affect numbers invalidates reuse. A hand-maintained per-workflow file list silently drifted
when new dependencies were added; deriving the set from the package layout cannot.
"""

from __future__ import annotations

import json
import platform
from functools import lru_cache
from pathlib import Path

from finance_quant.checksums import file_sha256, sha256_parts, sha256_text
from finance_quant.paths import dependency_lock

PACKAGE = Path(__file__).resolve().parent
# Presentation, persistence and process wiring cannot change research numbers.
EXCLUDED = {"web", "persistence", "api.py", "server.py", "research_api.py", "paths.py", "runtime.py"}
# Report wording is versioned separately so a text change re-renders reports without
# recomputing factors or backtests.
REPORT_MODULE = PACKAGE / "research" / "report.py"


def computation_sources() -> list[Path]:
    files = []
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE)
        if relative.parts[0] in EXCLUDED or "__pycache__" in relative.parts or path == REPORT_MODULE:
            continue
        files.append(path)
    return files


def report_code_sha256() -> str:
    return file_sha256(REPORT_MODULE)


@lru_cache(maxsize=1)
def _code_digest(signature: tuple[tuple[str, int, int], ...]) -> str:
    # Names are hashed with contents so a moved or renamed module also changes the digest.
    return sha256_parts(
        part for name, _, _ in signature for part in (name.encode(), b"\0", (PACKAGE / name).read_bytes(), b"\0")
    )


def code_sha256() -> str:
    files = computation_sources()
    signature = tuple(
        (path.relative_to(PACKAGE).as_posix(), path.stat().st_mtime_ns, path.stat().st_size) for path in files
    )
    return _code_digest(signature)


def source_provenance() -> dict:
    lock_path = dependency_lock()
    return {
        "code_sha256": code_sha256(),
        "dependency_lock_sha256": file_sha256(lock_path) if lock_path.exists() else None,
        "python_version": platform.python_version(),
    }


def fingerprint(provenance: dict) -> str:
    return sha256_text(json.dumps(provenance, sort_keys=True, allow_nan=False))[:12]
