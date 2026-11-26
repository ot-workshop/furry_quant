"""The immutable document for a restricted local price-return formula."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from finance_quant.checksums import canonical_json_sha256

MANUAL_FORMULA = "manual_formula"
PRICE_RETURN_VERSION = "price-return-v1"


@dataclass(frozen=True)
class FormulaVersion:
    window: int

    def __post_init__(self) -> None:
        if type(self.window) is not int or not 2 <= self.window <= 252:
            raise ValueError("Formula window must be an integer from 2 through 252")

    @classmethod
    def from_input(cls, document: Any) -> FormulaVersion:
        if not isinstance(document, dict) or set(document) != {"operator", "window"}:
            raise ValueError("Formula requires exactly the operator and window fields")
        if document["operator"] != "price_return":
            raise ValueError("Unsupported formula operator")
        return cls(document["window"])

    @classmethod
    def from_record(cls, document: Any) -> FormulaVersion:
        if not isinstance(document, dict):
            raise ValueError("Invalid formula version record")
        version = cls.from_input({key: document.get(key) for key in ("operator", "window")})
        if type(document.get("schema_version")) is not int or document != version.as_dict():
            raise ValueError("Formula version, evaluator or checksum mismatch")
        return version

    def _content(self) -> dict[str, Any]:
        return {
            "family": MANUAL_FORMULA,
            "schema_version": 1,
            "operation_version": PRICE_RETURN_VERSION,
            "operator": "price_return",
            "window": self.window,
        }

    @property
    def id(self) -> str:
        return "formula-" + canonical_json_sha256(self._content())

    def as_dict(self) -> dict[str, Any]:
        content = self._content()
        return {**content, "sha256": canonical_json_sha256(content)}
