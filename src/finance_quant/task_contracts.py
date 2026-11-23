"""Task queue contracts shared by submission, persistence and execution."""

import json
import re
from typing import Any, Protocol

from finance_quant.checksums import sha256_text

FORWARD_SCORE_MODE = "forward_score"


def submission_fingerprint(kind: str, request: dict, key: str) -> str:
    """Validate a submission key and fingerprint the original request, not its execution pins."""
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", key):
        raise ValueError("请求标识无效，请刷新后重新提交")
    encoded = json.dumps({"kind": kind, "request": request}, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return sha256_text(encoded)


class TaskConflict(ValueError):
    pass


class TaskStore(Protocol):
    """Storage operations needed by the task submission service."""

    def initialize(self) -> None: ...

    def get(self, identifier: str, *, private: bool = False) -> dict[str, Any]: ...

    def list(self) -> dict[str, Any]: ...

    def create(self, request: dict, execution: dict, key: str) -> dict[str, Any]: ...

    def find_experiment_replay(self, request: dict, key: str) -> dict[str, Any] | None:
        """Read an existing matching task; a miss must still go through atomic create_experiment."""
        ...

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
