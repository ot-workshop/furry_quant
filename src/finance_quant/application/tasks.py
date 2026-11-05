"""Task submission facade used by the HTTP API.

Execution always happens in the separate PostgreSQL worker (``finance_quant.server worker``).
The API only validates requests and records them in the injected task store.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from finance_quant.application.research_requests import validate_request


class TaskConflict(ValueError):
    pass


class TaskStore(Protocol):
    """Contract implemented by :class:`finance_quant.persistence.tasks.PgTaskStore`."""

    def initialize(self) -> None: ...

    def get(self, identifier: str, *, private: bool = False) -> dict[str, Any]: ...

    def list(self) -> dict[str, Any]: ...

    def create(self, request: dict, execution: dict, key: str) -> dict[str, Any]: ...

    def create_experiment(self, request: dict, execution: dict, key: str, *, total: int) -> dict[str, Any]: ...

    def create_download(
        self, kind: str, request: dict, execution: dict, key: str, *, total: int = 1
    ) -> dict[str, Any]: ...

    def claim(self, worker_id: str | None = None) -> str | None: ...

    def cancel(self, identifier: str) -> dict[str, Any]: ...

    def stage(self, identifier: str, stage: str, message: str, completed: int) -> None: ...

    def finish(
        self,
        identifier: str,
        status: str,
        *,
        result: dict | None = None,
        error: str | None = None,
        error_details: dict | None = None,
    ) -> None: ...


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
        from finance_quant.application.experiment_tasks import experiment_total, validate_experiment_request

        request, execution = validate_experiment_request(document, self.data_root, self.runs_root)
        return self.store.create_experiment(request, execution, key, total=experiment_total(request))
