"""Immutable, JSON-safe local research artifacts; independent of Qlib internals."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")


def runs_root() -> Path:
    return Path(os.environ.get("FINANCE_QUANT_RUNS", "runs")).resolve()


class ResultStore:
    def __init__(self, root: Path | None = None):
        self.root = (root or runs_root()).resolve()

    def path(self, category: str, run_id: str) -> Path:
        if category not in {"backtests", "factors", "evaluations", "experiments"} or not ID_PATTERN.fullmatch(run_id):
            raise ValueError("Invalid artifact identifier")
        result = (self.root / category / run_id / "result.json").resolve()
        if not result.is_relative_to(self.root):
            raise ValueError("Artifact outside result directory")
        return result

    def get(self, category: str, run_id: str) -> dict[str, Any]:
        return json.loads(self.path(category, run_id).read_text(encoding="utf-8"))

    def list(self, category: str) -> list[dict[str, Any]]:
        directory = self.root / category
        if not directory.exists():
            return []
        results = []
        for child in sorted(directory.iterdir()):
            if child.is_dir() and ID_PATTERN.fullmatch(child.name):
                try:
                    results.append(self.get(category, child.name))
                except FileNotFoundError:
                    # A task may have created its directory but not published yet.
                    continue
        return results

    def publish(self, category: str, run_id: str, document: dict[str, Any]) -> Path:
        """Publish a complete artifact atomically; never silently overwrite a run."""
        destination = self.path(category, run_id)
        encoded = json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2)
        return self._publish_text(destination, encoded, json_document=True)

    def publish_report(self, run_id: str, markdown: str, *, category: str = "evaluations") -> Path:
        return self._publish_text(self.path(category, run_id).with_name("report.md"), markdown)

    def publish_experiment_record(self, run_id: str, name: str, document: dict[str, Any]) -> Path:
        if name not in {"plan", "selection"}:
            raise ValueError("Unsupported experiment record")
        destination = self.path("experiments", run_id).with_name(name + ".json")
        return self._publish_text(destination, json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2), json_document=True)

    def _publish_text(self, destination: Path, encoded: str, *, json_document: bool = False) -> Path:
        def matches() -> bool:
            existing = destination.read_text(encoding="utf-8")
            return json.loads(existing) == json.loads(encoded) if json_document else existing == encoded

        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if matches():
                return destination
            raise FileExistsError(f"Artifact {destination.parent.name} already exists with different content")
        descriptor, staging = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            # A hard link makes creation atomic and refuses replacing an existing run.
            try:
                os.link(staging, destination)
            except FileExistsError:
                if not matches():
                    raise
        finally:
            Path(staging).unlink(missing_ok=True)
        return destination
