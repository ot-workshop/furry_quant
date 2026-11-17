"""Shared names and implementation version of statement-derived features."""

# Bundle schema and individual formulas are versioned separately: adding gross
# applicability details does not invalidate unchanged earnings-yield selections.
FEATURE_VERSION = "fundamental-ttm-v5"
STANDARD_FACTOR_VERSION = "fundamental-ttm-v4"
GROSS_MARGIN_VERSION = "gross-margin-industry-v1"
FEATURES = (
    "ttm_net_profit",
    "roe_ttm",
    "roa_ttm",
    "gross_margin_ttm",
    "asset_turnover_ttm",
    "debt_to_assets",
    "accruals_ttm",
    "profit_growth_ttm",
    "revenue_growth_ttm",
)
