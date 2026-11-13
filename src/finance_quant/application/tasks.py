"""Task submission facade used by the HTTP API.

Execution always happens in the separate PostgreSQL worker (``finance_quant.server worker``).
The API only validates requests and records them in the injected task store.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from finance_quant.application.experiment_tasks import experiment_total, validate_experiment_request
from finance_quant.application.research_requests import validate_request
from finance_quant.task_contracts import TaskConflict as TaskConflict
from finance_quant.task_contracts import TaskStore


class TaskService:
    def __init__(self, runs_root: Path, data_root: Path, *, task_store: TaskStore):
        self.runs_root, self.data_root = Path(runs_root).resolve(), Path(data_root).resolve()
        self.store = task_store

    def start(self) -> None:
        self.store.initialize()

    def close(self) -> None:
        """The API owns no worker resources."""

    def submit(self, document: dict, key: str) -> dict[str, Any]:
        request, execution = validate_request(document, self.data_root, self.runs_root)
        return self.store.create(request, execution, key)

    def submit_experiment(self, document: dict, key: str) -> dict[str, Any]:

        request, execution = validate_experiment_request(document, self.data_root, self.runs_root)
        return self.store.create_experiment(request, execution, key, total=experiment_total(request))
