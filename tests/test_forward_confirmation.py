"""Unit tests for the deterministic parts of forward registration (no database required)."""

from __future__ import annotations

from finance_quant.persistence import forward


def test_canonical_sha256_is_order_independent():
    a = forward.canonical_sha256({"b": 1, "a": [1, 2]})
    b = forward.canonical_sha256({"a": [1, 2], "b": 1})
    assert a == b
    assert len(a) == 64


def test_canonical_sha256_separates_different_payloads():
    assert forward.canonical_sha256({"top_k": 3}) != forward.canonical_sha256({"top_k": 4})


def test_confirmation_reports_failure_without_a_sample():
    result = forward.Confirmation(False, None, "attempt-missing")
    assert result.sealed is False
    assert result.sampled_at is None
    assert result.detail == "attempt-missing"
