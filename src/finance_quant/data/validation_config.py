"""Small, explicit policy for independent price verification."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CrosscheckConfig:
    mode: str = "off"

    def __post_init__(self):
        if not isinstance(self.mode, str) or self.mode not in {"off", "verify", "fallback"}:
            raise ValueError("data.validation.mode must be off, verify or fallback")

    def as_dict(self):
        return {"mode": self.mode}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) - {"mode"}:
            raise ValueError("data.validation must be an object containing only mode")
        return cls(**value)
