"""Task queue contracts shared by submission, persistence and execution."""

from typing import Any, Protocol


class TaskConflict(ValueError):
    pass


class TaskStore(Protocol):
    """Storage operations needed by the task submission service."""

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

    def save_prepared(self, identifier: str, execution: dict) -> None: ...

    def finish(
        self,
        identifier: str,
        status: str,
        *,
        result: dict | None = None,
        error: str | None = None,
        error_details: dict | None = None,
    ) -> None: ...
