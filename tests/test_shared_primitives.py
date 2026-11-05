from pathlib import Path

import pytest

from finance_quant.checksums import canonical_json_sha256, file_sha256, files_sha256, sha256_bytes, sha256_parts
from finance_quant.data.symbols import exchange_of, split_ts_code, to_baostock, to_qlib


def test_checksum_primitives_share_one_deterministic_policy(tmp_path: Path):
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"alpha")
    second.write_bytes(b"beta")

    assert file_sha256(first) == sha256_bytes(b"alpha")
    assert files_sha256([first, second]) == sha256_bytes(b"alphabeta")
    assert sha256_parts((b"alpha", b"beta")) == sha256_bytes(b"alphabeta")
    assert canonical_json_sha256({"b": 2, "a": 1}) == canonical_json_sha256({"a": 1, "b": 2})


def test_business_modules_do_not_implement_sha256_again():
    package = Path(__file__).parents[1] / "src/finance_quant"
    offenders = [
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if path.name != "checksums.py" and "import hashlib" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_security_code_conversions_are_validated_centrally():
    assert split_ts_code("600000.SH") == ("600000", "SH")
    assert to_qlib("000001.SZ") == "SZ000001"
    assert to_baostock("600000.SH") == "sh.600000"
    assert exchange_of("430047.BJ") == "BJ"
    with pytest.raises(ValueError, match="Invalid A-share code"):
        to_qlib("SH600000")
    with pytest.raises(ValueError, match="does not support"):
        to_baostock("430047.BJ")
