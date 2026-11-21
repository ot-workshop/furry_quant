"""Task submission facade used by the HTTP API.

Execution always happens in the separate PostgreSQL worker (``finance_quant.server worker``).
The API only validates requests and records them in the injected task store.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from finance_quant.application.experiment_tasks import (
    experiment_total,
    validate_experiment_document,
)
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

    def submit_experiment(self, document: dict, key: str, *, result_store=None) -> dict[str, Any]:

        # Cheap checks only: the expensive validation runs in the worker's prepare stage,
        # so a comparison submission returns as soon as it is queued.
        validate_experiment_document(document)
        execution = {"prepare_state": "pending"}
        return self.store.create_experiment(document, execution, key, total=experiment_total(document))
