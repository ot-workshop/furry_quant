import numpy as np
import pandas as pd
import pytest

from support.synthetic import sample_factors, sample_prices
from support.synthetic_qlib import export_sample


@pytest.mark.parametrize(
    "column,value", [("open", np.nan), ("open", 0), ("open", -1), ("close", np.nan), ("volume", 0), ("factor", 1.1)]
)
def test_invalid_execution_data_are_rejected_before_qlib_can_fallback(tmp_path, column, value):
    frame = sample_prices()
    frame.loc[0, column] = value
    with pytest.raises(ValueError):
        export_sample(frame, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


def test_missing_date_and_duplicate_bar_cannot_shift_feature_calendar(tmp_path):
    frame = sample_prices()
    with pytest.raises(ValueError, match="calendar"):
        export_sample(frame.drop(index=4), tmp_path / "gap")
    with pytest.raises(ValueError, match="Duplicate"):
        export_sample(pd.concat([frame, frame.iloc[[0]]]), tmp_path / "duplicate")


def test_too_little_history_is_an_explicit_error():
    frame = sample_prices()
    frame = frame[frame["date"] == frame["date"].min()]
    with pytest.raises(ValueError, match="history"):
        sample_factors(frame)
